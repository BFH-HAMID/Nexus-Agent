/* Shared HUD + mobile PWA. One authenticated SSE connection, no offline writes.
   Every data string is textContent, never HTML or instruction/permission authority. */
(() => {
  'use strict';
  const gui = window.nexusGui, i18n = window.nexusI18n;
  if (!gui || !i18n) return;
  const {t} = i18n, $ = id => document.getElementById(id);
  const root = document.createElement('section');
  root.id = 'view-presence'; root.className = 'gui-modules presence-page'; root.dataset.guiView = 'presence';
  root.hidden = gui.view !== 'presence';
  root.innerHTML = `
    <header class="ph-heading"><div><p class="ph-eyebrow" data-i18n="presence.eyebrow"></p><h1 data-i18n="presence.title"></h1><p data-i18n="presence.subtitle"></p></div>
      <div class="ph-actions"><button class="btn ghost" id="phLanguage" type="button">বাংলা / English</button><button class="btn" id="phReconnect" type="button" data-i18n="presence.reconnect"></button><button class="btn ghost" id="phInstall" type="button" hidden data-i18n="presence.install"></button></div></header>
    <p class="ph-connection" id="phConnection" role="status"></p><p id="phError" class="ph-error" role="alert" hidden></p>
    <div class="ph-layout"><div class="ph-column">
      <section class="ph-card ph-voice"><div class="ph-orb" id="phOrb" data-state="unknown" aria-hidden="true"><span></span></div>
        <div class="ph-voice-info"><p class="ph-eyebrow" data-i18n="presence.sharedVoice"></p><h2 id="phVoiceState"></h2><p id="phVoiceSource"></p><p id="phCapture"></p></div>
        <canvas id="phWave" width="640" height="90" data-i18n-aria="presence.wave" aria-label=""></canvas>
        <div class="ph-actions"><button class="btn" id="phPtt" type="button" data-i18n="presence.ptt"></button><button class="btn ghost" id="phTranscribe" type="button" data-i18n="presence.transcribe" disabled></button><button class="btn ghost" id="phStop" type="button" data-i18n="presence.stop"></button></div>
        <p class="ph-note" data-i18n="presence.micNote"></p><p id="phDraftStatus" role="status"></p>
      </section>
      <section class="ph-card"><header><h2 data-i18n="presence.conversation"></h2><span id="phConversationId" class="ph-id"></span></header>
        <div class="ph-transcript" id="phTranscript" aria-live="polite" aria-relevant="text additions"></div>
        <label class="ph-field"><span data-i18n="presence.draft"></span><textarea id="phDraft" maxlength="8000" data-i18n-ph="presence.placeholder"></textarea></label>
        <div class="ph-compose"><label><input id="phReadOnly" type="checkbox" checked> <span data-i18n="presence.readOnly"></span></label><button class="btn" id="phSend" type="button" data-i18n="presence.reviewTurn"></button></div>
        <p class="ph-note" data-i18n="presence.turnNote"></p>
      </section>
      <section class="ph-card"><header><h2 data-i18n="presence.missions"></h2><span id="phTokens" class="ph-id"></span></header><div id="phCost" class="ph-cost"></div><meter id="phCostMeter" min="0" hidden data-i18n-aria="presence.costMeter" aria-label=""></meter><div id="phMissions" class="ph-missions"></div></section>
    </div><div class="ph-column">
      <section class="ph-card"><header><h2 data-i18n="presence.approvals"></h2><span id="phApprovalCount" class="ph-badge">0</span></header><p class="ph-note" data-i18n="presence.approvalNote"></p><div id="phApprovals"></div></section>
      <section class="ph-card"><header><h2 data-i18n="presence.handoff"></h2></header>
        <label class="ph-field"><span data-i18n="presence.speaker"></span><select id="phSpeaker"></select></label>
        <label class="ph-field"><span data-i18n="presence.activeMission"></span><select id="phMission"></select></label>
        <div class="ph-actions"><button class="btn" id="phHandoff" type="button" data-i18n="presence.reviewHandoff"></button><button class="btn ghost" id="phListen" type="button" data-i18n="presence.listen"></button></div><p id="phCurrentSpeaker" class="ph-note"></p><p id="phSpeechStatus" class="ph-note"></p>
        <p class="ph-note" data-i18n="presence.handoffNote"></p><ul id="phDevices" class="ph-devices"></ul>
        <div id="phEnrollment"><label class="ph-field"><span data-i18n="presence.deviceName"></span><input id="phDeviceName" maxlength="60" autocomplete="off"></label><button class="btn ghost" id="phEnroll" type="button" data-i18n="presence.enroll"></button></div>
        <div id="phTokenBox" hidden><p class="ph-note" data-i18n="presence.tokenNote"></p><input id="phToken" type="password" readonly autocomplete="off" data-i18n-aria="presence.deviceToken" aria-label=""><div class="ph-actions"><button class="btn ghost" id="phCopy" type="button" data-i18n="presence.copy"></button><button class="btn ghost" id="phDismissToken" type="button" data-i18n="presence.dismiss"></button></div></div>
      </section>
      <section class="ph-card"><header><h2 data-i18n="presence.mobile"></h2></header><p class="ph-note" data-i18n="presence.pushNote"></p><p id="phService" class="ph-note"></p>
        <div class="ph-actions"><button class="btn ghost" id="phSubscribe" type="button" data-i18n="presence.subscribe"></button><button class="btn ghost" id="phUnsubscribe" type="button" data-i18n="presence.unsubscribe"></button><button class="btn ghost" id="phPush" type="button" data-i18n="presence.reviewPush"></button></div></section>
      <section class="ph-card"><header><h2 data-i18n="presence.briefs"></h2></header><div id="phBriefs"></div></section>
      <section class="ph-card"><header><h2 data-i18n="presence.world"></h2></header><p class="ph-note" data-i18n="presence.worldNote"></p><button class="btn ghost" id="phWorld" type="button" data-i18n="presence.openWorld"></button></section>
    </div></div><div id="phWorldHost"></div>`;
  (document.querySelector('main.deck') || document.body).after(root);
  const modal = document.createElement('dialog'); modal.id = 'phReview'; modal.className = 'ph-review';
  modal.innerHTML = `<form method="dialog"><h2 data-i18n="presence.exactReview"></h2><p data-i18n="presence.reviewNote"></p><pre id="phReviewText"></pre><p class="ph-confirm-code" id="phReviewCode"></p><label class="ph-field"><span data-i18n="presence.typeCode"></span><input id="phReviewInput" autocomplete="off"></label><div class="ph-actions"><button class="btn" id="phReviewConfirm" value="confirm" data-i18n="presence.confirm"></button><button class="btn ghost" value="cancel" formnovalidate data-i18n="presence.cancel"></button></div></form>`;
  document.body.append(modal);
  let resolveReview;
  modal.addEventListener('close', () => { resolveReview?.(modal.returnValue === 'confirm' && $('phReviewInput').value === $('phReviewCode').textContent); resolveReview = null; $('phReviewInput').value = ''; });
  modal.querySelector('form').addEventListener('submit', event => {
    if (event.submitter?.value === 'confirm' && $('phReviewInput').value !== $('phReviewCode').textContent) { event.preventDefault(); $('phReviewInput').setCustomValidity(t('presence.exactCode')); $('phReviewInput').reportValidity(); }
  });
  $('phReviewInput').oninput = () => $('phReviewInput').setCustomValidity('');
  const model = {config:null, data:null, connected:false, visible:gui.view === 'presence', controller:null, timer:0, cursor:0,
    speaking:false, audio:null, audioURL:'', wantsMic:false, micPromise:null, wave:0, speechSeen:new Set(), rendered:{}, install:null};
  const make = (tag, cls='', text) => { const node=document.createElement(tag); node.className=cls; if(text!==undefined)node.textContent=text; return node; };
  const api = (path, body) => gui.api('/api/presence/' + path, body);
  function error(value) { $('phError').textContent=value?.message || String(value || ''); $('phError').hidden=!value; }
  async function guard(work) { try { error(null); return await work(); } catch(e) { error(e); return null; } }
  function allowed(scope) { return model.connected && (model.config?.principal?.kind === 'operator' || model.config?.principal?.scopes?.includes(scope)); }
  async function confirm(text, code) {
    if(modal.open)return false;
    $('phReviewText').textContent=text; $('phReviewCode').textContent=code; $('phReviewInput').value=''; $('phReviewInput').setCustomValidity(''); modal.returnValue=''; modal.showModal(); $('phReviewInput').focus();
    return new Promise(resolve => { resolveReview=resolve; });
  }
  async function reviewed(operation, args) {
    const review=await api('review',{operation,arguments:args});
    if(!model.connected || !await confirm(JSON.stringify(review.arguments,null,2),review.confirmation) || !model.connected)return null;
    const result=await api('execute',{id:review.id,confirmation:review.confirmation});
    return result; // Never retry a failed execute: it may already have had an effect.
  }
  function stable(key, value) { const digest=JSON.stringify(value); if(model.rendered[key]===digest)return false; model.rendered[key]=digest; return true; }
  function labels() { i18n.apply(root); i18n.apply(modal); model.rendered={}; paint(); }
  function controls() {
    const cfg=model.config || {};
    for(const [id, scope, enabled] of [['phSend','chat',cfg.conversation_enabled],['phHandoff','handoff',cfg.handoff_enabled],
      ['phPtt','voice',cfg.voice_enabled],['phWorld','read',cfg.world_enabled],['phListen','voice',cfg.voice_enabled],['phEnroll','read',cfg.devices_enabled],
      ['phSubscribe','push',cfg.push_enabled],['phPush','push',cfg.push_enabled],['phUnsubscribe','push',cfg.push_enabled]]) $(id).disabled=!allowed(scope)||!enabled;
    $('phTranscribe').disabled=!allowed('voice') || !cfg.voice_enabled || !window.nexusAssistant?.draftAudio?.();
    $('phEnrollment').hidden=cfg.principal?.kind !== 'operator';
    $('phConnection').textContent=!cfg.enabled?t('presence.off'):model.connected?t('presence.live'):t('presence.disconnected');
    $('phConnection').dataset.live=String(model.connected);
    $('phListen').disabled ||= cfg.principal?.kind !== 'device';
    $('phSubscribe').disabled ||= cfg.principal?.kind !== 'device';
    $('phUnsubscribe').disabled ||= cfg.principal?.kind !== 'device';
    root.querySelectorAll('[data-approval-action]').forEach(n=>n.disabled=!allowed('approve'));
  }
  function options(id, rows, current, key) {
    if(!stable(key,rows)){if(!model.handoffDirty)$(id).value=current;return;}
    const select=$(id), previous=select.value;
    select.replaceChildren(...rows.map(([value,label])=>{const option=make('option','',label);option.value=value;return option;}));
    select.value=model.handoffDirty && rows.some(r=>r[0]===previous)?previous:current;
  }
  function paint() {
    controls(); const data=model.data;
    if(!data){$('phVoiceState').textContent=t('presence.unknown');$('phOrb').dataset.state='unknown';return;}
    const voice=data.voice || {}, turns=data.turns || [], active=turns.find(r=>['queued','thinking','running'].includes(r.status));
    const state=!model.connected?'unknown':model.wantsMic?'listening':model.audio?'speaking':voice.state && voice.state !== 'idle'?voice.state:active?'thinking':'idle';
    $('phOrb').dataset.state=state;$('phVoiceState').textContent=t('presence.'+state);
    $('phVoiceSource').textContent=voice.source?`${t('presence.source')}: ${voice.source}${voice.stale?' · '+t('presence.stale'):''}`:t('presence.noMic');
    const v=data.vision;
    $('phCapture').textContent=v?`${t('presence.camera')}: ${v.camera?.state || t('presence.unknown')} · ${t('presence.screen')}: ${v.screen?.state || t('presence.unknown')}${v.stopped?' · '+t('presence.hardOff'):''}`:t('presence.captureUnknown');
    $('phConversationId').textContent=(data.conversation_id || '').slice(0,12);
    if(stable('turns',turns)) {
      const transcript=$('phTranscript');transcript.replaceChildren();
      if(!turns.length)transcript.append(make('p','ph-empty',t('presence.noTurns')));
      for(const row of turns){
        const item=make('article','ph-turn'); item.append(make('p','ph-message ph-user',row.user));
        item.append(make('p','ph-message',row.assistant || t('presence.waiting')));
        item.append(make('small','ph-note',`${row.source} · ${row.status}`)); transcript.append(item);
      }
    }
    if(voice.transcript && stable('voiceDraft',voice.transcript))$('phDraftStatus').textContent=t('presence.liveDraft')+': '+voice.transcript;
    const tokens=turns.reduce((sum,r)=>sum+(r.usage?.input_tokens||0)+(r.usage?.output_tokens||0),0);
    $('phTokens').textContent=t('presence.tokens',{count:tokens});
    const mission=(data.missions||[]).find(m=>m.id===data.active_mission);
    $('phCost').textContent=mission?`$${Number(mission.cost_usd).toFixed(4)} / $${Number(mission.cost_cap_usd).toFixed(2)} · ${t('presence.recordedEstimate')}`:t('presence.costUnknown');
    const validCost=mission && Number.isFinite(mission.cost_usd) && Number.isFinite(mission.cost_cap_usd) && mission.cost_cap_usd>0;
    $('phCostMeter').hidden=!validCost;if(validCost){$('phCostMeter').max=mission.cost_cap_usd;$('phCostMeter').value=mission.cost_usd;}
    if(stable('missions',data.missions)){
      const host=$('phMissions');host.replaceChildren();
      for(const m of data.missions||[]){
        const block=make('article','ph-mission');block.append(make('h3','',m.goal),make('p','ph-note',`${m.id} · ${m.status}`));
        const nodes=make('ol','ph-dag');
        for(const n of m.nodes||[]){const node=make('li','ph-node');node.dataset.state=n.status;node.append(make('b','',n.title||n.id),make('span','ph-badge',n.status),make('small','ph-note',`${n.id} ← ${(n.depends_on||[]).join(', ')||'—'}`));nodes.append(node);}block.append(nodes);host.append(block);
      }
      if(!host.children.length)host.append(make('p','ph-empty',t('presence.noMissions')));
    }
    const approvals=data.approvals || [];$('phApprovalCount').textContent=approvals.length;
    if(stable('approvals',approvals.map(r=>[r.id,r.action_text,r.decisions,r.complete,r.token]))){
      const host=$('phApprovals');host.replaceChildren();
      for(const row of approvals){
        const card=make('article','ph-approval');card.append(make('h3','',row.tool),make('p','ph-note',row.title),make('pre','ph-exact',row.action_text));
        if(!row.complete)card.append(make('p','ph-error',t('presence.incomplete')));
        const buttons=make('div','ph-actions');
        for(const choice of row.decisions || []){
          const button=make('button','btn '+(['deny','reject','cancel'].includes(choice)?'ghost':''),t('presence.'+choice));button.type='button';button.dataset.approvalAction=choice;
          button.disabled=!allowed('approve');
          button.onclick=()=>guard(async()=>{
            button.disabled=true;
            try {
              const grant=['approve','allow'].includes(choice);
              if(grant && (!row.complete || !await confirm(row.action_text,row.token)))return;
              await api('approval',{source:row.source,id:row.reference,...(row.approval_id?{approval_id:row.approval_id}:{}),decision:choice,confirmation:grant?row.token:''});
              card.remove(); model.rendered.approvals='';
            } finally { button.disabled=!allowed('approve'); }
          });buttons.append(button);
        }card.append(buttons);host.append(card);
      }
      if(!approvals.length)host.append(make('p','ph-empty',t('presence.noApprovals')));
    }
    const devices=data.devices || [], live=devices.filter(d=>!d.revoked && d.expires>Date.now()/1000);
    options('phSpeaker',[['none',t('presence.none')],['local',t('presence.local')],...live.filter(d=>d.scopes?.includes('voice')).map(d=>[d.id,d.name])],data.speaker,'speakers');
    options('phMission',[['',t('presence.none')],...(data.missions||[]).map(m=>[m.id,m.goal])],data.active_mission,'missionChoices');
    if(stable('devices',devices.map(d=>[d.id,d.name,d.revoked,d.expires,d.scopes,d.push_subscribed]))){
      $('phDevices').replaceChildren(...devices.map(d=>{
        const li=make('li');li.append(make('span','',d.name+' · '+(d.revoked?t('presence.revoked'):(d.scopes||[]).join(', '))));
        if(!d.revoked && (data.principal?.kind==='operator' || d.id===data.principal?.id)){
          const button=make('button','btn ghost',t('presence.revoke'));button.type='button';button.onclick=()=>guard(async()=>{await api('revoke',{id:d.id});button.disabled=true;});li.append(button);
        }return li;
      }));
    }
    if(stable('briefs',data.briefs)){
      $('phBriefs').replaceChildren(...(data.briefs||[]).map(r=>make('p','ph-message',r.text)));
      if(!$('phBriefs').children.length)$('phBriefs').append(make('p','ph-empty',t('presence.noBriefs')));
    }
    $('phSpeechStatus').textContent=model.speaking?t('presence.speechArmed'):t('presence.speechOff');
    $('phCurrentSpeaker').textContent=t('presence.currentSpeaker')+': '+(devices.find(d=>d.id===data.speaker)?.name || t('presence.'+data.speaker));
    $('phService').textContent=t('presence.service')+': '+(data.service?.state || t('presence.unknown'));
    if(model.audio && (data.speaker!==data.principal?.id || data.speaker_generation!==model.audioGeneration))stopAudio();
    consumeSpeech();
  }
  async function consumeSpeech() {
    if(!model.speaking || model.audio || model.claiming || !model.connected)return;
    const row=(model.data?.speech||[]).find(r=>!model.speechSeen.has(r.id));if(!row)return;
    model.speechSeen.add(row.id);if(model.speechSeen.size>128)model.speechSeen.delete(model.speechSeen.values().next().value);
    model.claiming=true;
    try {
      const clip=await api('speech',{id:row.id});
      if(!model.connected || !model.speaking || model.data.speaker_generation!==clip.generation)return;
      const blob=new Blob([Uint8Array.from(atob(clip.audio_base64),c=>c.charCodeAt(0))],{type:clip.format==='mp3'?'audio/mpeg':'audio/wav'});
      model.audioURL=URL.createObjectURL(blob);model.audio=new Audio(model.audioURL);model.audioGeneration=clip.generation;
      model.audio.onended=()=>{stopAudio();if(model.connected)api('voice/state',{state:'idle'}).catch(()=>{});consumeSpeech();};
      await model.audio.play(); api('voice/state',{state:'speaking'}).catch(()=>{}); paint();
    } catch(e){stopAudio();model.speaking=false;error(e);} finally {model.claiming=false;}
  }
  function stopAudio(){if(model.audio){model.audio.pause();model.audio=null;}if(model.audioURL)URL.revokeObjectURL(model.audioURL);model.audioURL='';}
  function stopAll(){model.wantsMic=false;model.speaking=false;window.nexusAssistant?.stopRecording?.();window.nexusAssistant?.stop?.();stopAudio();model.speaking=false;if(model.wave)cancelAnimationFrame(model.wave);model.wave=0;}
  function accept(payload){
    if(!payload?.state)return;model.data=payload.state;model.cursor=payload.state.revision;model.connected=true;
    model.config={...model.config,...payload.state.capabilities,principal:payload.state.principal || model.config?.principal};
    if(!model.config.enabled){model.connected=false;clearPrivateView();paint();return;}
    if(!model.config.voice_enabled || !allowed('voice')){stopAll();window.nexusAssistant?.discardAudio?.();}
    if(!model.config.world_enabled && model.worldOpen){model.worldBound=false;$('phWorldHost').hidden=true;window.dispatchEvent(new Event('hud:disconnected'));}
    paint();if(model.worldOpen && model.config.world_enabled && !model.worldBound)guard(mountWorld);window.dispatchEvent(new CustomEvent('nexus:presence',{detail:payload.state}));
  }
  async function connect(){
    if(model.controller || !model.visible)return;
    clearTimeout(model.timer);
    const controller=new AbortController();model.controller=controller;
    try {
      await gui.ready;
      if(controller.signal.aborted || !model.visible)return;
      try {model.config=await api('config');}catch(e){clearPrivateView();throw e;}controls();if(!model.config.enabled){clearPrivateView();return;}
      if(controller.signal.aborted || !model.visible)return;
      const response=await gui.fetch('/api/presence/events',{signal:controller.signal,headers:{'Last-Event-ID':String(model.cursor)}});
      if(!response.ok || !response.body){clearPrivateView();throw Error(t('presence.disconnected'));}
      const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='';
      let watchdog=setTimeout(()=>controller.abort(),25000);
      try{
        while(!controller.signal.aborted){
          const chunk=await reader.read();if(chunk.done)break;
          clearTimeout(watchdog);watchdog=setTimeout(()=>controller.abort(),25000);
          buffer+=decoder.decode(chunk.value,{stream:true});
          if(buffer.length>2000000)throw Error(t('presence.streamLimit'));
          let end;
          while((end=buffer.indexOf('\n\n'))>=0){
            const frame=buffer.slice(0,end);buffer=buffer.slice(end+2);
            const name=frame.split('\n').find(l=>l.startsWith('event: '))?.slice(7);
            const body=frame.split('\n').find(l=>l.startsWith('data: '))?.slice(6);
            if(name==='presence.revoked'){model.config.enabled=false;clearPrivateView();throw Error(t('presence.revoked'));}
            if(name==='presence.state' && body)accept(JSON.parse(body));
          }
        }
      } finally {clearTimeout(watchdog);reader.cancel().catch(()=>{});}
    } catch(e){if(e.name!=='AbortError')error(e);} finally {
      model.controller=null;model.connected=false;model.worldBound=false;window.dispatchEvent(new Event('hud:disconnected'));stopAll();paint();
      if(model.visible && model.config?.enabled)model.timer=setTimeout(connect,5000);
    }
  }
  function clearPrivateView(){
    model.data=null;model.rendered={};model.speechSeen.clear();stopAll();window.nexusAssistant?.discardAudio?.();
    for(const id of ['phTranscript','phApprovals','phMissions','phBriefs','phDevices','phDraftStatus','phVoiceSource','phCapture','phCost','phCurrentSpeaker','phSpeechStatus','phConversationId','phTokens','phSpeaker','phMission','phService'])$(id).replaceChildren();
    for(const id of ['phDraft','phToken'])$(id).value='';$('phTokenBox').hidden=true;$('phCostMeter').hidden=true;
    if(modal.open)modal.close('cancel');$('phReviewText').textContent='';$('phReviewCode').textContent='';model.worldBound=false;$('phWorldHost').hidden=true;if(model.worldOpen)window.dispatchEvent(new Event('hud:disconnected'));
  }
  function drawWave(){
    const canvas=$('phWave'),ctx=canvas.getContext('2d');if(!ctx)return;
    const samples=window.nexusAssistant?.waveform?.();ctx.clearRect(0,0,canvas.width,canvas.height);ctx.strokeStyle=samples?'#42dcb0':'#52736b';ctx.lineWidth=2;ctx.beginPath();
    if(samples)samples.forEach((v,i)=>{const x=i/(samples.length-1)*canvas.width,y=v/255*canvas.height;i?ctx.lineTo(x,y):ctx.moveTo(x,y);});
    else {ctx.moveTo(0,canvas.height/2);ctx.lineTo(canvas.width,canvas.height/2);}ctx.stroke();
    if(model.wantsMic)model.wave=requestAnimationFrame(drawWave);else model.wave=0;
  }
  async function micStart(){
    if(!allowed('voice') || !model.config?.voice_enabled || model.wantsMic || model.micPromise)return;
    model.wantsMic=true;stopAudio();
    model.micPromise=guard(async()=>{
      if(!window.nexusAssistant?.startRecording)throw Error(t('presence.micUnavailable'));
      await window.nexusAssistant.startRecording();
      if(!model.wantsMic){window.nexusAssistant.stopRecording();return;}
      await api('voice/state',{state:'listening'});drawWave();paint();
    });
    await model.micPromise;model.micPromise=null;
    if(!window.nexusAssistant?.state?.().microphone){model.wantsMic=false;paint();}
  }
  function micStop(){model.wantsMic=false;window.nexusAssistant?.stopRecording?.();const active=allowed('voice') && model.config?.voice_enabled;if(active)api('voice/state',{state:'idle'}).catch(()=>{});else window.nexusAssistant?.discardAudio?.();drawWave();controls();}
  $('phPtt').onpointerdown=e=>{e.preventDefault();e.currentTarget.setPointerCapture(e.pointerId);micStart();};
  $('phPtt').onpointerup=micStop;$('phPtt').onpointercancel=micStop;
  $('phPtt').onkeydown=e=>{if([' ','Enter'].includes(e.key)){e.preventDefault();micStart();}};
  $('phPtt').onkeyup=e=>{if([' ','Enter'].includes(e.key)){e.preventDefault();micStop();}};
  $('phPtt').onblur=micStop;
  $('phTranscribe').onclick=()=>guard(async()=>{
    const audio=window.nexusAssistant?.draftAudio?.();if(!audio)return;
    if(!await confirm(t('presence.transcribeNote'),'TRANSCRIBE AUDIO'))return;
    const result=await api('voice/transcribe',{audio_base64:audio,confirmation:'TRANSCRIBE AUDIO'});
    if(!model.connected || !model.config?.voice_enabled || !allowed('voice'))return;
    $('phDraft').value=result.transcript; $('phDraftStatus').textContent=t('presence.draftOnly');
  });
  $('phSend').onclick=()=>guard(async()=>{const text=$('phDraft').value;const result=await reviewed('chat',{text,read_only:$('phReadOnly').checked});if(result){if($('phDraft').value===text)$('phDraft').value='';$('phDraftStatus').textContent=t('presence.queued');}});
  $('phSpeaker').onchange=$('phMission').onchange=()=>{model.handoffDirty=true;};
  $('phHandoff').onclick=()=>guard(async()=>{if(await reviewed('handoff',{speaker:$('phSpeaker').value,mission:$('phMission').value}))model.handoffDirty=false;});
  $('phListen').onclick=async()=>{model.speaking=!model.speaking;if(!model.speaking)stopAudio();paint();};
  $('phStop').onclick=()=>{stopAll();if(model.connected)api('voice/state',{state:'idle'}).catch(()=>{});paint();};
  $('phEnroll').onclick=()=>guard(async()=>{const result=await reviewed('enroll',{name:$('phDeviceName').value});if(result){$('phToken').value=result.token;$('phTokenBox').hidden=false;}});
  $('phCopy').onclick=()=>guard(()=>navigator.clipboard.writeText($('phToken').value));
  $('phDismissToken').onclick=()=>{$('phToken').value='';$('phTokenBox').hidden=true;};
  $('phPush').onclick=()=>guard(()=>reviewed('push',{device:$('phSpeaker').value}));
  $('phSubscribe').onclick=()=>guard(async()=>{
    if(!('serviceWorker' in navigator) || !('PushManager' in window))throw Error(t('presence.pushUnavailable'));
    const key=await api('push-key');
    if(await Notification.requestPermission()!=='granted')throw Error(t('presence.pushUnavailable'));
    const registration=await navigator.serviceWorker.register('/assistant-sw.js');await navigator.serviceWorker.ready;
    const raw=key.public_key.replace(/-/g,'+').replace(/_/g,'/');const bytes=Uint8Array.from(atob(raw+'='.repeat((4-raw.length%4)%4)),c=>c.charCodeAt(0));
    const prior=await registration.pushManager.getSubscription();
    const sub=prior || await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:bytes});
    try {const result=await reviewed('subscribe',{subscription:sub.toJSON()});if(!result&&!prior)await sub.unsubscribe();}
    catch(e){if(!prior)await sub.unsubscribe();throw e;}
  });
  $('phUnsubscribe').onclick=()=>guard(async()=>{await api('unsubscribe',{});const reg=await navigator.serviceWorker?.getRegistration();await (await reg?.pushManager.getSubscription())?.unsubscribe();});
  $('phReconnect').onclick=()=>{clearTimeout(model.timer);if(model.controller)model.controller.abort();else connect();};
  async function mountWorld(){if(model.config?.world_enabled && model.connected && gui.mountWorld && !model.worldBinding){model.worldBinding=true;try{$('phWorldHost').hidden=false;model.worldBound=(await gui.mountWorld($('phWorldHost'),path=>api('world/'+path)))!==false;model.worldOpen=true;}finally{model.worldBinding=false;}}}
  $('phWorld').onclick=()=>guard(mountWorld);
  $('phLanguage').onclick=()=>i18n.setLang(i18n.lang==='bn'?'en':'bn');
  window.addEventListener('beforeinstallprompt',event=>{event.preventDefault();model.install=event;$('phInstall').hidden=false;});
  $('phInstall').onclick=async()=>{await model.install?.prompt();model.install=null;$('phInstall').hidden=true;};
  window.addEventListener('nexus:view',({detail})=>{model.visible=detail==='presence';if(model.visible){connect();}else {clearTimeout(model.timer);model.controller?.abort();stopAll();}});
  window.addEventListener('nexus:microphone',({detail})=>{if(!detail.active){model.wantsMic=false;drawWave();paint();}});
  window.addEventListener('nexus:authenticated',()=>{if(model.visible)connect();});
  window.addEventListener('pagehide',()=>{model.visible=false;clearTimeout(model.timer);model.controller?.abort();stopAll();$('phToken').value='';});
  i18n.onChange(labels);labels();drawWave();
  if(model.visible)connect();
  // Integration hooks carry state only, not a way to execute or grant consent.
  window.nexusPresence={accept,stop:stopAll,state:()=>({connected:model.connected,cursor:model.cursor,speaking:model.speaking}),
    setConfig:cfg=>{model.config=cfg;controls();}};
})();
