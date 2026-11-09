/* Opt-in personal assistant. Real API state only; no fabricated hardware health.
   Microphone samples stay in this tab until Transcribe/Verify is pressed. API,
   audio and personal content are never written to the service-worker cache. */
(() => {
  'use strict';
  const gui = window.nexusGui;
  if (!gui) return;
  const $ = id => document.getElementById(id);
  const root = document.createElement('section');
  root.id = 'view-assistant'; root.className = 'gui-modules assistant-page'; root.dataset.guiView = 'assistant';
  root.hidden = gui.view !== 'assistant';
  root.innerHTML = `
    <header class="assistant-head">
      <div class="assistant-headtext"><p class="assistant-eyebrow">Private by default · ব্যক্তিগত নিয়ন্ত্রণ</p>
        <h1>Your personal assistant</h1>
        <p class="assistant-sub">Voice, vision, durable memory and proactive work—wired to the same authenticated Nexus server. Hardware, cloud accounts and mutations stay inactive until you explicitly use them.</p></div>
      <div class="assistant-headtools"><span class="assistant-chip" id="asPrivacy">opt-in</span>
        <button class="btn ghost assistant-install" id="asInstall" type="button" hidden>Install app</button>
        <button class="btn" id="asRefresh" type="button">Refresh</button></div>
    </header>
    <div class="assistant-kpis" id="asKpis"></div><p class="assistant-error" id="asError" role="alert" hidden></p>
    <div class="assistant-layout"><div class="assistant-column">
      <section class="assistant-card assistant-voice"><div class="assistant-cardhead"><h2>Voice · বাংলা + English</h2><span class="assistant-chip" id="asVoiceChip">microphone off</span></div>
        <div class="assistant-cardbody"><p class="assistant-note">Start captures a real waveform locally. Nothing is uploaded until you choose Transcribe or Verify. Verification is not approval; CLI voice actions require an enrolled speaker and a fresh spoken challenge.</p>
          <div class="assistant-wave-wrap"><canvas id="assistantWave" width="800" height="140" aria-label="Live microphone or playback waveform"></canvas><span class="assistant-wave-label" id="asWaveLabel">idle · no synthetic waveform</span></div>
          <div class="assistant-row"><button class="btn" id="asMicStart" type="button">Start microphone</button><button class="btn ghost" id="asMicStop" type="button" disabled>Stop & keep draft</button>
            <button class="btn ghost" id="asTranscribe" type="button" disabled>Transcribe</button><button class="btn ghost" id="asVerify" type="button" disabled>Verify speaker</button></div>
          <div class="assistant-grid" style="margin-top:12px"><label class="assistant-field assistant-field-wide"><span>Transcript / text to speak</span><textarea id="asVoiceText" maxlength="8000" placeholder="Speak, or type বাংলা / English text…"></textarea></label>
            <label class="assistant-field"><span>Speech engine</span><select id="asTtsEngine"><option value="piper">Piper · offline</option><option value="kokoro">Kokoro · offline</option><option value="edge">Edge · Bengali online</option><option value="elevenlabs">ElevenLabs · online</option></select></label>
            <label class="assistant-field"><span>Server Whisper model</span><input id="asWhisperModel" value="small" maxlength="500" aria-describedby="asVoiceState"></label></div>
          <div class="assistant-row"><button class="btn" id="asSpeak" type="button">Speak text</button><button class="btn ghost" id="asStopSpeech" type="button">Barge-in / stop</button><button class="btn ghost" id="asUseDraft" type="button">Use as task draft</button></div>
          <label class="assistant-note" style="margin-top:10px"><input type="checkbox" id="asLiveVoice"> Speak local streaming answers from the first sentence (Piper/Kokoro only)</label>
          <p class="assistant-audio-state" id="asVoiceState" role="status">No microphone permission requested.</p></div></section>

      <section class="assistant-card"><div class="assistant-cardhead"><h2>Personal memory</h2><span class="assistant-chip" id="asMemoryChip">disabled</span></div>
        <div class="assistant-cardbody"><p class="assistant-note">A separate workspace-local store for preferences, routines and current context. It is never enabled by task learning. Every save, edit and forget action is confirmed and audited.</p>
          <div class="assistant-row"><button class="btn" id="asMemoryToggle" type="button">Enable personal memory</button><button class="btn ghost danger" id="asForgetAll" type="button">Forget all</button></div>
          <div class="assistant-grid" style="margin-top:12px"><label class="assistant-field assistant-field-wide"><span>New preference, routine or fact</span><textarea id="asMemoryText" maxlength="12000"></textarea></label>
            <label class="assistant-field"><span>Kind</span><select id="asMemoryKind"><option value="preference">preference</option><option value="routine">routine</option><option value="fact">fact</option><option value="note">note</option></select></label>
            <label class="assistant-field"><span>Vector / text search</span><input id="asMemoryQuery" placeholder="What do you remember about…"></label></div>
          <div class="assistant-row"><button class="btn" id="asMemoryAdd" type="button">Remember</button><button class="btn ghost" id="asMemorySearch" type="button">Search</button></div>
          <div class="assistant-grid" style="margin-top:12px"><label class="assistant-field"><span>Preferred language profile</span><input id="asLanguage" placeholder="bn-BD or en-US"></label>
            <label class="assistant-field"><span>Current work across sessions</span><input id="asActiveTask" readonly></label></div>
          <div class="assistant-row"><button class="btn ghost" id="asSaveLanguage" type="button">Save language</button></div>
          <ul class="assistant-list" id="asMemoryRows"></ul></div></section>

      <section class="assistant-card"><div class="assistant-cardhead"><h2>Documents, images & cited research</h2><span class="assistant-chip">no tools for source text</span></div>
        <div class="assistant-cardbody"><p class="assistant-note">Vision is OFF until enabled in operator-owned vision.yaml. Uploads stay in memory; image and document text is untrusted data. Cloud requires a fresh exact review and no people. Camera/face pipelines are local-only.</p>
          <div class="assistant-grid"><label class="assistant-field assistant-field-wide"><span>Image or document (10 MB max)</span><input id="asUpload" type="file" accept=".png,.jpg,.jpeg,.webp,.txt,.md,.csv,.json,.log,.pdf"></label>
            <label class="assistant-field"><span>Processing location</span><select id="asVisionLocation"><option value="local">Local model (YAML)</option><option value="cloud">Gemini (explicit YAML opt-in)</option></select></label>
            <label class="assistant-field"><span>PDF pages (optional)</span><input id="asVisionPages" placeholder="1,3"></label>
            <label class="assistant-field assistant-field-wide"><span>Question</span><textarea id="asVisionQuestion">Answer only from this upload. What does it say?</textarea></label></div>
          <label class="assistant-note"><input id="asVisionNoPeople" type="checkbox"> I attest cloud uploads contain no people/faces; detection is best effort</label><label class="assistant-field"><span>Region (optional normalized left,top,right,bottom)</span><input id="asVisionRegion" placeholder="0,0,1,1"></label><div class="assistant-row"><button class="btn" id="asAnalyze" type="button">Analyze upload</button><button class="btn ghost" id="asVisionVoice" type="button">Use voice question</button><button class="btn danger" id="asVisionOff" type="button">Vision hard off</button><span id="asVisionStatus" role="status">Camera / screen unknown</span></div><pre class="assistant-output" id="asVisionOut" hidden></pre>
          <hr style="border:0;border-top:1px solid var(--line,rgba(148,180,169,.15));margin:18px 0">
          <div class="assistant-grid"><label class="assistant-field assistant-field-wide"><span>Research question</span><input id="asResearchQuestion"></label>
            <label class="assistant-field assistant-field-wide"><span>Exact HTTPS URLs, one per line</span><textarea id="asResearchUrls" placeholder="https://example.com/source"></textarea></label>
            <label class="assistant-field assistant-field-wide"><span>Allowed domains, comma separated</span><input id="asResearchDomains" placeholder="example.com"></label></div>
          <div class="assistant-row"><button class="btn" id="asResearch" type="button">Research with citations</button></div><pre class="assistant-output" id="asResearchOut" hidden></pre></div></section>
    </div><div class="assistant-column">
      <section class="assistant-card"><div class="assistant-cardhead"><h2>Reminders & monitors</h2><span class="assistant-chip" id="asScheduler">worker off</span></div>
        <div class="assistant-cardbody"><p class="assistant-note">Durable schedules reuse the existing inbox and DND policy. Start the explicit CLI/systemd worker for background checks; this page never claims an inactive worker is running.</p>
          <label class="assistant-field"><span>Reminder</span><input id="asReminderText" maxlength="1000" placeholder="Check the deployment"></label>
          <div class="assistant-grid" style="margin-top:9px"><label class="assistant-field"><span>Repeat every minutes</span><input id="asReminderEvery" type="number" min="1" max="10080" value="30"></label>
            <label class="assistant-field"><span>Timezone</span><input id="asTimezone" value="UTC"></label></div>
          <div class="assistant-row"><button class="btn" id="asReminderAdd" type="button">Add reminder</button></div><ul class="assistant-list" id="asReminderRows"></ul>
          <div class="assistant-grid" style="margin-top:15px"><label class="assistant-field"><span>Monitor kind</span><select id="asMonitorKind"><option value="server">Server</option><option value="website">Website</option><option value="market">Market JSON</option><option value="github_ci">GitHub CI</option></select></label>
            <label class="assistant-field"><span>Every minutes</span><input id="asMonitorEvery" type="number" min="1" max="1440" value="5"></label>
            <label class="assistant-field assistant-field-wide"><span>Monitor options (JSON)</span><textarea id="asMonitorOptions">{"metric":"memory","threshold":90}</textarea></label></div>
          <div class="assistant-row"><button class="btn" id="asMonitorAdd" type="button">Add monitor</button><button class="btn ghost" id="asSchedulerPass" type="button">Run one pass</button></div><ul class="assistant-list" id="asMonitorRows"></ul></div></section>

      <section class="assistant-card"><div class="assistant-cardhead"><h2>Morning brief</h2><span class="assistant-chip">weather · calendar · email · news</span></div>
        <div class="assistant-cardbody"><p class="assistant-note">Each source reports disabled/unavailable honestly. Optional model summary receives untrusted evidence and no tools.</p>
          <div class="assistant-grid"><label class="assistant-field"><span>Local time</span><input id="asBriefTime" type="time" value="08:00"></label><label class="assistant-field"><span>Timezone</span><input id="asBriefZone" value="UTC"></label></div>
          <div class="assistant-row"><button class="btn" id="asBriefBuild" type="button">Build now</button><button class="btn ghost" id="asBriefSummarize" type="button">Build + summarize</button><button class="btn ghost" id="asBriefSchedule" type="button">Enable schedule</button></div><pre class="assistant-output" id="asBriefOut" hidden></pre></div></section>

      <section class="assistant-card"><div class="assistant-cardhead"><h2>Personal data & email review</h2><span class="assistant-chip">zero implicit sends</span></div>
        <div class="assistant-cardbody"><p class="assistant-note">Google and Notion tokens come only from environment/native keyring. Obsidian reads an explicit vault. Email, events and notes are data, never commands.</p>
          <div class="assistant-grid"><label class="assistant-field"><span>Source</span><select id="asDataSource"><option>gmail</option><option>calendar</option><option>contacts</option><option>drive</option><option>notion</option><option>obsidian</option></select></label>
            <label class="assistant-field"><span>Query</span><input id="asDataQuery" value="newer_than:1d"></label><label class="assistant-field assistant-field-wide"><span>Obsidian vault (obsidian only)</span><input id="asDataVault" placeholder="/path/to/vault"></label></div>
          <div class="assistant-row"><button class="btn" id="asDataRead" type="button">Read personal data</button></div><pre class="assistant-output" id="asDataOut" hidden></pre>
          <div class="assistant-grid" style="margin-top:14px"><label class="assistant-field assistant-field-wide"><span>Email recipient</span><input id="asEmailTo" type="email" autocomplete="off"></label>
            <label class="assistant-field assistant-field-wide"><span>Subject</span><input id="asEmailSubject" maxlength="200"></label><label class="assistant-field assistant-field-wide"><span>Body</span><textarea id="asEmailBody" maxlength="20000"></textarea></label></div>
          <div class="assistant-row"><button class="btn ghost" id="asDraftPreview" type="button">Preview draft</button><button class="btn" id="asSendPreview" type="button">Preview send</button><button class="btn" id="asEmailExecute" type="button" disabled>Execute reviewed email</button><button class="btn ghost" id="asVoiceEmail" type="button" disabled>Voice approve</button></div><p class="assistant-note" id="asEmailState" style="margin-top:9px">No pending email challenge.</p></div></section>

      <section class="assistant-card"><div class="assistant-cardhead"><h2>Backups, access & sync</h2><span class="assistant-chip" id="asSync">not synced yet</span></div>
        <div class="assistant-cardbody"><p class="assistant-note">Backups need operator-enabled file roots in permissions.yaml and exclude private paths. Backup and restore buttons only queue a request: review it in Approvals, then inspect the final run result. Restore never overwrites; its destination must be inside an allowed root. Phone and laptop share state through this server.</p>
          <div class="assistant-row"><button class="btn" id="asBackup" type="button">Queue backup</button><button class="btn ghost" id="asOpenComputer" type="button">Desktop controls</button></div>
          <div class="assistant-grid" style="margin-top:10px"><label class="assistant-field"><span>Backup id</span><input id="asBackupId"></label><label class="assistant-field"><span>New restore directory</span><input id="asRestoreTarget" value="restored"></label></div>
          <div class="assistant-row"><button class="btn ghost" id="asRestore" type="button">Queue restore without overwrite</button><button class="btn ghost" id="asAutomationApprovals" type="button">Open approvals</button></div><pre class="assistant-output" id="asBackupOut" hidden></pre>
          <div class="assistant-sync" style="margin-top:14px"><span id="asSyncDetail">Authenticated revision polling · personal responses not cached offline</span><span id="asRevision">r0</span></div></div></section>

      <section class="assistant-card"><div class="assistant-cardhead"><h2>Capability boundaries</h2><span class="assistant-chip">adapters ≠ verified hardware</span></div><div class="assistant-cardbody"><div class="assistant-capabilities" id="asCapabilities"></div></div></section>
    </div></div>`;
  (document.querySelector('main.deck') || document.body).after(root);

  const dialog = document.createElement('dialog'); dialog.className = 'assistant-confirm';
  dialog.innerHTML = `<form method="dialog"><h2>Confirm the exact action</h2><p>This mutation or external request will be audited. Type the phrase exactly; no permission is remembered.</p><p class="assistant-confirm-code" id="asConfirmPhrase"></p>
    <input class="assistant-confirm-input" id="asConfirmInput" autocomplete="off" required><div class="assistant-row"><button class="btn" value="confirm">Confirm</button><button class="btn ghost" value="cancel" formnovalidate>Cancel</button></div></form>`;
  document.body.append(dialog);
  let confirmResolve = null;
  dialog.addEventListener('close', () => { if (confirmResolve) confirmResolve(dialog.returnValue === 'confirm' ? $('asConfirmInput').value : null); confirmResolve = null; });
  function confirmation(phrase) {
    if (dialog.open) return Promise.resolve(null);
    $('asConfirmPhrase').textContent = phrase; $('asConfirmInput').value = ''; dialog.returnValue = '';
    dialog.showModal(); $('asConfirmInput').focus();
    return new Promise(resolve => { confirmResolve = resolve; });
  }
  const state = {overview:null, visible:false, cursor:0, pendingEmail:null, audioBase64:'', audioName:'recording.wav',
    mic:null, audioContext:null, analyser:null, processor:null, chunks:[], waveFrame:0, playback:null,
    liveBuffer:'', liveQueue:[], liveBusy:false, liveEnabled:false, installPrompt:null};
  const el = (tag, cls, text) => { const n=document.createElement(tag); if(cls)n.className=cls; if(text!==undefined)n.textContent=text; return n; };
  const output = (id, value) => { const n=$(id); n.textContent=typeof value==='string'?value:JSON.stringify(value,null,2); n.hidden=false; };
  function error(value) { const n=$('asError'); n.textContent=value?.message||String(value||''); n.hidden=!value; }
  async function guard(work) { try { error(null); return await work(); } catch(e) { error(e); return null; } }
  async function action(path, phrase, body={}) { const code=await confirmation(phrase); if(code!==phrase)return null; return gui.api('/api/assistant/'+path,{...body,confirmation:code}); }
  const chip = (text,tone='') => { const n=el('span','assistant-chip',text); if(tone)n.dataset.tone=tone; return n; };

  async function loadOverview(poll=false) {
    const suffix=poll&&state.cursor?'?since='+encodeURIComponent(state.cursor):'';
    const data=await gui.api('/api/assistant/overview'+suffix);
    if(!poll||data.memory_changed||!state.overview) state.overview=data;
    else state.overview={...state.overview,...data,memory:state.overview.memory};
    state.cursor=Number(data.sync?.cursor||state.cursor||0); paint(); return data;
  }
  function paint() {
    const data=state.overview;if(!data)return;const memory=data.memory||{};
    $('asKpis').replaceChildren(...[
      ['Personal memory',memory.enabled?'enabled':'disabled'],['Entries',String(memory.entries?.length??'—')],
      ['Reminders',String(data.reminders?.length||0)],['Monitors',String(data.monitors?.length||0)]].map(([label,value])=>{
        const n=el('div','assistant-kpi');n.append(el('b','',value),el('span','',label));return n;}));
    $('asMemoryChip').textContent=memory.enabled?'enabled · opt-in':'disabled';
    $('asMemoryChip').dataset.tone=memory.enabled?'ok':'warn';
    $('asMemoryToggle').textContent=memory.enabled?'Disable personal memory':'Enable personal memory';
    $('asLanguage').value=memory.profile?.language||$('asLanguage').value;
    $('asActiveTask').value=memory.active?.task||'No active context saved';
    $('asScheduler').textContent=data.scheduler?.running?'worker running':'worker off · explicit start required';
    $('asScheduler').dataset.tone=data.scheduler?.running?'ok':'warn';
    $('asBriefTime').value=data.brief_schedule?.time||'08:00';$('asBriefZone').value=data.brief_schedule?.timezone||'UTC';
    $('asBriefSchedule').textContent=data.brief_schedule?.enabled?'Disable schedule':'Enable schedule';
    $('asRevision').textContent='r'+state.cursor;$('asSync').textContent='revision synchronized';
    renderMemory(memory.entries||[]);renderReminders(data.reminders||[]);renderMonitors(data.monitors||[]);renderCapabilities(data.capabilities?.groups||[]);
  }
  function renderMemory(rows) {
    const list=$('asMemoryRows');list.replaceChildren();if(!rows.length){list.append(el('li','assistant-empty','No matching personal entries.'));return;}
    for(const row of rows){const item=el('li','assistant-item');const top=el('div','assistant-item-top');
      top.append(chip(row.kind||'note'),el('small','',row.id+(row.score!==undefined?' · '+Number(row.score).toFixed(3):'')));
      const text=el('textarea');text.value=row.text||'';text.maxLength=12000;text.setAttribute('aria-label','Memory '+row.id);
      const actions=el('div','assistant-item-actions');const save=el('button','btn ghost small','Save edit');save.type='button';
      save.onclick=()=>guard(async()=>{await action('memory/entry','EDIT PERSONAL MEMORY '+row.id,{id:row.id,text:text.value,kind:row.kind});await loadOverview();});
      const forget=el('button','btn ghost small','Forget');forget.type='button';forget.onclick=()=>guard(async()=>{await action('memory/forget','FORGET PERSONAL MEMORY '+row.id,{id:row.id});await loadOverview();});
      actions.append(save,forget);item.append(top,text,actions);list.append(item);}
  }
  function renderReminders(rows) {
    const list=$('asReminderRows');list.replaceChildren();if(!rows.length){list.append(el('li','assistant-empty','No durable reminders.'));return;}
    for(const row of rows.slice(0,30)){const item=el('li','assistant-item');const top=el('div','assistant-item-top');top.append(el('b','',row.text),el('small','',new Date(row.next_run*1000).toLocaleString()));
      const cancel=el('button','btn ghost small','Cancel');cancel.type='button';cancel.onclick=()=>guard(async()=>{await action('reminders/cancel','CANCEL REMINDER '+row.id,{id:row.id});await loadOverview();});
      item.append(top,cancel);list.append(item);}
  }
  function renderMonitors(rows) {
    const list=$('asMonitorRows');list.replaceChildren();if(!rows.length){list.append(el('li','assistant-empty','No saved monitors.'));return;}
    for(const row of rows.slice(0,30)){const item=el('li','assistant-item');const top=el('div','assistant-item-top');top.append(chip(row.kind),el('small','',row.id+' · '+row.every_minutes+'m'));
      item.append(top,el('small','',JSON.stringify(row.options||{})));if(row.last_result)item.append(el('p','assistant-note',JSON.stringify(row.last_result)));
      const remove=el('button','btn ghost small','Remove');remove.type='button';remove.onclick=()=>guard(async()=>{await action('monitors/remove','REMOVE MONITOR '+row.id,{id:row.id});await loadOverview();});item.append(remove);list.append(item);}
  }
  function renderCapabilities(groups) { const host=$('asCapabilities');host.replaceChildren();for(const group of groups){const n=el('div','assistant-capability');n.append(el('b','',group.title||group.id),el('span','',(group.features||[]).length+' adapters · explicit activation'));host.append(n);} }

  /* Actual microphone waveform and client-side WAV packaging. */
  function drawWave() {
    const canvas=$('assistantWave'),ctx=canvas.getContext('2d'),w=canvas.width,h=canvas.height;ctx.clearRect(0,0,w,h);
    ctx.strokeStyle='rgba(45,212,164,.16)';ctx.lineWidth=1;for(let y=20;y<h;y+=25){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke();}
    const analyser=state.analyser;
    if(!analyser){ctx.strokeStyle='#53756b';ctx.beginPath();ctx.moveTo(0,h/2);ctx.lineTo(w,h/2);ctx.stroke();state.waveFrame=0;return;}
    const values=new Uint8Array(analyser.fftSize);analyser.getByteTimeDomainData(values);ctx.strokeStyle='#2dd4a4';ctx.lineWidth=2;ctx.beginPath();
    values.forEach((value,index)=>{const x=index/(values.length-1)*w,y=value/255*h;if(index===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);});ctx.stroke();
    state.waveFrame=requestAnimationFrame(drawWave);
  }
  function waveformFrom(context,source,label) { if(state.waveFrame)cancelAnimationFrame(state.waveFrame);state.audioContext=context;state.analyser=context.createAnalyser();state.analyser.fftSize=1024;source.connect(state.analyser);$('asWaveLabel').textContent=label;drawWave(); }
  function stopWave(label='idle · no synthetic waveform') { if(state.waveFrame)cancelAnimationFrame(state.waveFrame);state.waveFrame=0;state.analyser=null;$('asWaveLabel').textContent=label;drawWave(); }
  function encodeWav(samples,rate) {
    const targetRate=16000,ratio=rate/targetRate,length=Math.floor(samples.length/ratio),pcm=new Int16Array(length);
    for(let i=0;i<length;i++){const start=Math.floor(i*ratio),end=Math.min(samples.length,Math.floor((i+1)*ratio));let sum=0,count=0;for(let j=start;j<Math.max(start+1,end);j++){sum+=samples[j]||0;count++;}const sample=Math.max(-1,Math.min(1,sum/Math.max(1,count)));pcm[i]=sample<0?sample*32768:sample*32767;}
    const buffer=new ArrayBuffer(44+pcm.length*2),view=new DataView(buffer);const text=(offset,value)=>{for(let i=0;i<value.length;i++)view.setUint8(offset+i,value.charCodeAt(i));};
    text(0,'RIFF');view.setUint32(4,36+pcm.length*2,true);text(8,'WAVEfmt ');view.setUint32(16,16,true);view.setUint16(20,1,true);view.setUint16(22,1,true);view.setUint32(24,targetRate,true);view.setUint32(28,targetRate*2,true);view.setUint16(32,2,true);view.setUint16(34,16,true);text(36,'data');view.setUint32(40,pcm.length*2,true);new Int16Array(buffer,44).set(pcm);
    let binary='';const bytes=new Uint8Array(buffer);for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));return btoa(binary);
  }
  /* Every acquisition error stops already-granted tracks before it is reported. */
  let micGeneration=0, micPending=false;
  async function acquireMic() {
    if(state.mic || micPending)return false;
    const generation=++micGeneration;micPending=true;let stream,context;
    state.chunks=[];state.audioBase64='';
    try {
      stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true},video:false});
      if(generation!==micGeneration){stream.getTracks().forEach(track=>track.stop());return false;}
      context=new (window.AudioContext||window.webkitAudioContext)();await context.resume();
      if(generation!==micGeneration){stream.getTracks().forEach(track=>track.stop());await context.close();return false;}
      const source=context.createMediaStreamSource(stream),processor=context.createScriptProcessor(4096,1,1),mute=context.createGain();mute.gain.value=0;
      source.connect(processor);processor.connect(mute);mute.connect(context.destination);processor.onaudioprocess=e=>{if(state.chunks.length<1100)state.chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));};
      state.mic=stream;state.processor=processor;state.audioContext=context;waveformFrom(context,source,'live microphone · local only');
      $('asMicStart').disabled=true;$('asMicStop').disabled=false;$('asTranscribe').disabled=true;$('asVerify').disabled=true;$('asVoiceChip').textContent='recording · max 30 s';$('asVoiceState').textContent='Recording locally. Stop before transcription or verification.';
      window.dispatchEvent(new CustomEvent('nexus:microphone',{detail:{active:true}}));
      setTimeout(()=>{if(state.mic===stream)stopMic();},30000);return true;
    } catch(e) {stream?.getTracks().forEach(track=>track.stop());context?.close().catch(()=>{});throw e;}
    finally {micPending=false;}
  }
  function stopMic() {
    ++micGeneration; // A late getUserMedia grant must not revive a released PTT.
    const stream=state.mic;if(!stream)return null;state.mic=null;stream.getTracks().forEach(track=>track.stop());state.processor?.disconnect();
    const rate=state.audioContext?.sampleRate||48000,length=state.chunks.reduce((n,a)=>n+a.length,0),samples=new Float32Array(length);let offset=0;for(const chunk of state.chunks){samples.set(chunk,offset);offset+=chunk.length;}
    state.audioContext?.close().catch(()=>{});state.audioContext=null;state.processor=null;state.chunks=[];stopWave('recording ready · not uploaded');
    state.audioBase64=samples.length?encodeWav(samples,rate):'';$('asMicStart').disabled=false;$('asMicStop').disabled=true;$('asTranscribe').disabled=!state.audioBase64;$('asVerify').disabled=!state.audioBase64;$('asVoiceChip').textContent='draft audio ready';$('asVoiceState').textContent='WAV draft is in this tab only. Choose exactly what to send.';
    window.dispatchEvent(new CustomEvent('nexus:microphone',{detail:{active:false}}));return state.audioBase64||null;
  }
  async function playClip(clip) {
    if(state.mic)stopMic();stopSpeech(false);const bytes=Uint8Array.from(atob(clip.audio_base64),c=>c.charCodeAt(0));const blob=new Blob([bytes],{type:clip.format==='mp3'?'audio/mpeg':'audio/wav'}),url=URL.createObjectURL(blob),audio=new Audio(url);state.playback={audio,url};
    const context=new (window.AudioContext||window.webkitAudioContext)();await context.resume();const source=context.createMediaElementSource(audio);source.connect(context.destination);waveformFrom(context,source,'live speech output · barge-in available');state.playback.context=context;
    audio.onended=()=>{URL.revokeObjectURL(url);context.close().catch(()=>{});if(state.playback?.audio===audio)state.playback=null;stopWave();runLiveQueue();};await audio.play();
  }
  function stopSpeech(disableLive=true) {
    if(disableLive){state.liveEnabled=false;$('asLiveVoice').checked=false;state.liveQueue=[];state.liveBuffer='';state.liveBusy=false;}
    if(state.playback){state.playback.audio.pause();state.playback.audio.currentTime=0;URL.revokeObjectURL(state.playback.url);state.playback.context?.close().catch(()=>{});state.playback=null;}stopWave('speech interrupted');
  }
  async function synthesize(text,automatic=false) {
    const engine=$('asTtsEngine').value;if(automatic&& !['piper','kokoro'].includes(engine))throw Error('Live streaming speech is restricted to offline Piper/Kokoro; use manual confirmation for online TTS.');
    const phrase=['edge','elevenlabs'].includes(engine)?'UPLOAD TEXT TO TTS':'SPEAK TEXT';
    const body={text,engine};let result;
    if(automatic&&state.liveEnabled)result=await gui.api('/api/assistant/voice/speak',{...body,confirmation:'SPEAK TEXT'});
    else result=await action('voice/speak',phrase,body);
    if(result)await playClip(result);return result;
  }
  function queueLiveSentences(final=false) {
    const matches=[...state.liveBuffer.matchAll(/[.!?।]["'”’)\]]*\s+/g)];let consumed=0;
    for(const match of matches){const end=match.index+match[0].length,sentence=state.liveBuffer.slice(consumed,end).trim();if(sentence&&state.liveQueue.length<8)state.liveQueue.push(sentence);consumed=end;}
    state.liveBuffer=state.liveBuffer.slice(consumed);if(final&&state.liveBuffer.trim()){state.liveQueue.push(state.liveBuffer.trim());state.liveBuffer='';}runLiveQueue();
  }
  async function runLiveQueue() {
    if(state.liveBusy||state.playback||!state.liveEnabled||!state.liveQueue.length)return;state.liveBusy=true;const sentence=state.liveQueue.shift();
    try{await synthesize(sentence,true);}catch(e){error(e);state.liveEnabled=false;$('asLiveVoice').checked=false;state.liveQueue=[];}finally{state.liveBusy=false;if(!state.playback&&state.liveQueue.length)runLiveQueue();}
  }

  $('asMicStart').onclick=()=>guard(acquireMic);$('asMicStop').onclick=stopMic;
  $('asTranscribe').onclick=()=>guard(async()=>{const audio=state.audioBase64||stopMic();if(!audio)throw Error('Record audio first.');const result=await action('voice/transcribe','TRANSCRIBE AUDIO',{audio_base64:audio,model:$('asWhisperModel').value});if(result){$('asVoiceText').value=result.transcript;$('asVoiceState').textContent='Transcript ['+result.language+'] is a draft only; speaker is not verified and no task ran.';}});
  $('asVerify').onclick=()=>guard(async()=>{const audio=state.audioBase64||stopMic();if(!audio)throw Error('Record audio first.');const result=await action('voice/verify','VERIFY MY VOICE',{audio_base64:audio});if(result)$('asVoiceState').textContent='Speaker match='+result.matched+' score='+Number(result.score).toFixed(3)+'. This alone authorizes nothing.';});
  $('asSpeak').onclick=()=>guard(()=>synthesize($('asVoiceText').value));$('asStopSpeech').onclick=()=>stopSpeech();
  $('asUseDraft').onclick=()=>{const text=$('asVoiceText').value.trim();if(!text)return;$('taskInput').value=text;gui.show('run');$('taskInput').focus();};
  $('asLiveVoice').onchange=async e=>{if(!e.target.checked){stopSpeech();return;}if(!['piper','kokoro'].includes($('asTtsEngine').value)){e.target.checked=false;error(Error('Select offline Piper or Kokoro for live streaming speech.'));return;}const code=await confirmation('SPEAK TEXT');state.liveEnabled=code==='SPEAK TEXT';e.target.checked=state.liveEnabled;if(!state.liveEnabled)return;$('asVoiceState').textContent='Local first-sentence speech enabled for this tab. Stop revokes it immediately.';};
  $('asTtsEngine').onchange=()=>{if(state.liveEnabled&&!['piper','kokoro'].includes($('asTtsEngine').value))stopSpeech();};
  window.addEventListener('nexus:event',({detail})=>{if(!state.liveEnabled)return;if(detail?.type==='text_delta'&&detail.text){state.liveBuffer+=detail.text;queueLiveSentences();}else if(detail?.type==='done')queueLiveSentences(true);});

  $('asRefresh').onclick=()=>guard(()=>loadOverview());
  $('asMemoryToggle').onclick=()=>guard(async()=>{const enabled=!state.overview?.memory?.enabled;await action('memory/enable',enabled?'SAVE PERSONAL MEMORY':'DISABLE PERSONAL MEMORY',{enabled});await loadOverview();});
  $('asMemoryAdd').onclick=()=>guard(async()=>{await action('memory/entry','ADD PERSONAL MEMORY',{text:$('asMemoryText').value,kind:$('asMemoryKind').value});$('asMemoryText').value='';await loadOverview();});
  $('asMemorySearch').onclick=()=>guard(async()=>{const data=await gui.api('/api/assistant/memory?q='+encodeURIComponent($('asMemoryQuery').value));state.overview.memory=data;paint();});
  $('asForgetAll').onclick=()=>guard(async()=>{await action('memory/forget','FORGET ALL PERSONAL MEMORY',{all:true});await loadOverview();});
  $('asSaveLanguage').onclick=()=>guard(async()=>{await action('memory/profile','SAVE PERSONAL PROFILE',{key:'language',value:$('asLanguage').value});await loadOverview();});
  $('asReminderAdd').onclick=()=>guard(async()=>{await action('reminders/add','ADD REMINDER',{text:$('asReminderText').value,every_minutes:Number($('asReminderEvery').value),timezone:$('asTimezone').value});$('asReminderText').value='';await loadOverview();});
  $('asMonitorKind').onchange=()=>{const examples={server:{metric:'memory',threshold:90},website:{url:'https://example.com/health',status:200},market:{url:'https://example.com/quote',field:'quote.price',threshold:10,direction:'above'},github_ci:{repo:'owner/repository',ref:'HEAD'}};$('asMonitorOptions').value=JSON.stringify(examples[$('asMonitorKind').value]);};
  $('asMonitorAdd').onclick=()=>guard(async()=>{await action('monitors/add','ADD MONITOR',{kind:$('asMonitorKind').value,options:JSON.parse($('asMonitorOptions').value),every_minutes:Number($('asMonitorEvery').value)});await loadOverview();});
  $('asSchedulerPass').onclick=()=>guard(async()=>{output('asBriefOut',await action('scheduler/pass','RUN ASSISTANT SCHEDULER',{}));await loadOverview();});
  $('asBriefBuild').onclick=()=>guard(async()=>output('asBriefOut',await action('brief/build','BUILD MORNING BRIEF',{})));
  $('asBriefSummarize').onclick=()=>guard(async()=>output('asBriefOut',await action('brief/build','SUMMARIZE MORNING BRIEF',{summarize:true})));
  $('asBriefSchedule').onclick=()=>guard(async()=>{const enabled=!state.overview?.brief_schedule?.enabled;await action('brief/schedule',enabled?'SCHEDULE MORNING BRIEF':'DISABLE MORNING BRIEF',{enabled,time:$('asBriefTime').value,timezone:$('asBriefZone').value});await loadOverview();});

  function fileBase64(file) { if(!file||file.size>10000000)throw Error('Choose one file no larger than 10 MB.');return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onerror=()=>reject(Error('Could not read upload'));reader.onload=()=>resolve(String(reader.result).slice(String(reader.result).indexOf(',')+1));reader.readAsDataURL(file);}); }
  async function refreshVision() {
    try { const v=await gui.api('/api/vision/status');$('asVisionStatus').textContent=`Camera: ${v.camera.state} · Screen: ${v.screen.state}${v.stopped?' · HARD OFF / stop requested':''}${v.enabled===false?' · policy OFF':''}${v.screen.last_capture?' · last screen '+new Date(v.screen.last_capture*1000).toLocaleTimeString():''}`; }
    catch (_) { $('asVisionStatus').textContent='Camera / screen UNKNOWN · authenticated server required'; }
  }
  $('asVisionOff').onclick=()=>guard(async()=>{await gui.api('/api/vision/off',{});await refreshVision();});
  $('asVisionVoice').onclick=()=>{ $('asVisionQuestion').value=$('asVoiceText').value; };
  $('asAnalyze').onclick=()=>guard(async()=>{
    const file=$('asUpload').files[0];if(!file)throw Error('Choose an image/PDF first.');
    const encoded=await fileBase64(file),cloud=$('asVisionLocation').value==='cloud';
    const numbers=id=>$(id).value.trim()?$(id).value.split(',').map(x=>Number(x.trim())):null;
    const body={name:file.name,file_base64:encoded,question:$('asVisionQuestion').value,cloud,no_people:$('asVisionNoPeople').checked,pages:numbers('asVisionPages'),region:numbers('asVisionRegion')};
    if(cloud){
      if(!body.no_people)throw Error('No people/faces may be uploaded. Use local vision if unsure.');
      const preview=await gui.api('/api/vision/preview',body);output('asVisionOut',preview.review);
      const code=await confirmation(preview.challenge.confirmation);if(code!==preview.challenge.confirmation)return;
      body.challenge=preview.challenge;body.confirmation=code;
      body.second_factor=prompt('Separate permissions.yaml PIN if required; otherwise leave blank.')||'';
    }
    output('asVisionOut',await gui.api('/api/vision/ask',body));$('asUpload').value='';await refreshVision();
  });
  setInterval(()=>{if(state.visible&&!document.hidden)refreshVision();},2000);

  $('asResearch').onclick=()=>guard(async()=>{const urls=$('asResearchUrls').value.split(/\n/).map(x=>x.trim()).filter(Boolean),allowed_domains=$('asResearchDomains').value.split(',').map(x=>x.trim()).filter(Boolean);output('asResearchOut',await action('research','RESEARCH WEB',{question:$('asResearchQuestion').value,urls,allowed_domains}));});

  function emailBody(stage) { return {to:$('asEmailTo').value,subject:$('asEmailSubject').value,body:$('asEmailBody').value}; }
  async function previewEmail(stage) { const label=stage==='send'?'SEND EMAIL':'DRAFT EMAIL',body=emailBody(stage);const result=await action('email/'+stage+'/preview','PREVIEW '+label,body);if(result){state.pendingEmail={stage,body,challenge:result.challenge};$('asEmailExecute').disabled=false;$('asVoiceEmail').disabled=false;$('asEmailState').textContent=label+' preview ready; no external write yet. Fresh code: '+result.challenge.confirmation+' — type it, or record “হ্যাঁ চালাও '+result.challenge.confirmation+'” and use Voice approve.';} }
  $('asDraftPreview').onclick=()=>guard(()=>previewEmail('draft'));$('asSendPreview').onclick=()=>guard(()=>previewEmail('send'));
  $('asEmailExecute').onclick=()=>guard(async()=>{const pending=state.pendingEmail;if(!pending)return;const code=pending.challenge.confirmation,result=await action('email/'+pending.stage+'/execute',code,{...pending.body,challenge:pending.challenge});if(result){$('asEmailState').textContent='External write acknowledged by Google. Review the returned result; replay of this challenge is refused.';state.pendingEmail=null;$('asEmailExecute').disabled=true;$('asVoiceEmail').disabled=true;output('asDataOut',result);}});
  $('asVoiceEmail').onclick=()=>guard(async()=>{const pending=state.pendingEmail;if(!pending)return;const audio=state.audioBase64||stopMic();if(!audio)throw Error('Record your fresh voice decision first.');const result=await gui.api('/api/assistant/voice/email',{...pending.body,stage:pending.stage,challenge:pending.challenge,audio_base64:audio,model:$('asWhisperModel').value});output('asDataOut',result);if(result.decision?.approved){state.pendingEmail=null;$('asEmailExecute').disabled=true;$('asVoiceEmail').disabled=true;$('asEmailState').textContent='Verified speaker and exact challenge accepted; Google write executed in the same server request.';}else{$('asEmailState').textContent='Voice approval refused or cancelled; no email was written.';}});
  $('asDataRead').onclick=()=>guard(async()=>{const source=$('asDataSource').value,result=await action('data/'+source,'READ PERSONAL DATA',{query:$('asDataQuery').value,vault:$('asDataVault').value});output('asDataOut',result);});
  function queuedAutomation(result) {
    if(!result)return;
    output('asBackupOut',`Task ${result.task_id} queued for fresh approval, not a completed backup/restore. Open Approvals and inspect the final result in Runs.\n`+JSON.stringify(result,null,2));
  }
  $('asBackup').onclick=()=>guard(async()=>{const result=await action('automation/backup','CREATE BACKUP',{});if(result)$('asBackupId').value=result.id;queuedAutomation(result);});
  $('asRestore').onclick=()=>guard(async()=>{const id=$('asBackupId').value;queuedAutomation(await action('automation/restore','RESTORE BACKUP '+id,{id,target:$('asRestoreTarget').value}));});
  $('asAutomationApprovals').onclick=()=>gui.show('approvals');
  $('asOpenComputer').onclick=()=>gui.show('computer');

  window.addEventListener('beforeinstallprompt',event=>{event.preventDefault();state.installPrompt=event;$('asInstall').hidden=false;});
  $('asInstall').onclick=async()=>{if(!state.installPrompt)return;await state.installPrompt.prompt();state.installPrompt=null;$('asInstall').hidden=true;};
  if('serviceWorker' in navigator)navigator.serviceWorker.register('/assistant-sw.js').catch(()=>{ $('asSyncDetail').textContent='Online sync works; offline app-shell registration is unavailable.'; });
  window.addEventListener('nexus:view',event=>{state.visible=event.detail==='assistant';if(state.visible){guard(()=>loadOverview());refreshVision();}});
  setInterval(()=>{if(state.visible&&!document.hidden)guard(()=>loadOverview(true));},20000);
  window.addEventListener('pagehide',()=>{if(state.mic)stopMic();stopSpeech();});
  if(gui.view==='assistant'){guard(()=>loadOverview());refreshVision();}
  window.nexusAssistant={load:loadOverview,state:()=>({microphone:!!state.mic,draftAudio:!!state.audioBase64,liveVoice:state.liveEnabled,pendingEmail:!!state.pendingEmail,cursor:state.cursor}),
    startRecording:acquireMic,stopRecording:stopMic,draftAudio:()=>state.audioBase64,
    discardAudio:()=>{stopMic();state.audioBase64='';state.chunks=[];},
    waveform:()=>{if(!state.analyser)return null;const values=new Uint8Array(state.analyser.fftSize);state.analyser.getByteTimeDomainData(values);return values;},
    stop:()=>{stopMic();stopSpeech();}};
})();
