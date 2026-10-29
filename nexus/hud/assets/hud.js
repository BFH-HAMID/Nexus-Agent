(() => {
'use strict';
const $ = id => document.getElementById(id);
let authenticated = false, runtimeModelDirty = false, pendingControl = null;
let token = '', socket = null, generation = 0, selectedAction = null, latest = null;
const rendered = {};
const busyButtons = new WeakSet();
function controlState() {
  if (!latest) return;
  const state = latest.daemon.state;
  const allowed = {start: !['running','starting','paused','unresponsive'].includes(state) && latest.policy.level !== 'L0', pause: state === 'running', resume: state === 'paused' && latest.policy.level !== 'L0', stop: !['stopped','killed'].includes(state), kill: !['stopped','killed'].includes(state)};
  for (const n of document.querySelectorAll('[data-control]')) { n.disabled = !authenticated || busyButtons.has(n) || !allowed[n.dataset.control]; n.title = !authenticated ? 'Connect first' : allowed[n.dataset.control] ? 'Request ' + n.dataset.control : 'Not available while worker is ' + state; }
}
function changed(key, value) { const next = JSON.stringify(value); if (rendered[key] === next) return false; rendered[key] = next; return true; }
function access(connected) {
  for (const n of document.querySelectorAll('[data-control], #autonomy, #quiet, #dnd, #dnd-off, #runtime-enable, #runtime-disable, #delegate button, #jobs button, #approvals button, #allow, #deny')) n.disabled = !connected;
  $('connection-help').textContent = window.nexusGui ? (connected ? 'Connected through the same Nexus GUI session. Control background tasks here; World, news and weather use the World tab.' : 'Connecting through your existing Nexus GUI session…') : connected ? 'Connected. Queue a task, review approvals and control the worker here. World and weather are further down on this same page.' : 'Connect with your private token to enable controls. Remember this browser to reconnect automatically.';
}
const message = text => { $('error').textContent = text; };
async function api(path, body) {
  if (window.nexusGui) return window.nexusGui.api('/api/operations/' + path, body);
  if (!token && !authenticated) throw Error('Connect using your private HUD token first.');
  const response = await fetch('/api/' + path, {method: body === undefined ? 'GET' : 'POST', headers: {...(token ? {Authorization: 'Bearer ' + token} : {}), 'Content-Type': 'application/json'}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
  const value = await response.json();
  if (!response.ok) throw Error(value.detail || value.error || 'Request refused. Inspect state and retry.');
  return value;
}
async function perform(operation, body) {
  const value = await api('action/' + operation, body);
  pendingControl = operation === 'control' ? {action: body.action, expected: {start:'running',resume:'running',pause:'paused',stop:'stopped',kill:'killed'}[body.action]} : null;
  $('detail').textContent = JSON.stringify(value, null, 2);
  $('action-status').textContent = value.confirmed === false ? 'Request saved. Waiting for the worker to acknowledge; check Worker status above.' : 'Action saved. Current state is refreshed below.';
  paint(await api('state'));
  return value;
}
function bind(button, fn) {
  button.addEventListener('click', async () => {
    busyButtons.add(button); button.disabled = true; message('');
    try { await fn(); } catch (error) { message(error.message); }
    finally { busyButtons.delete(button); button.disabled = false; controlState(); }
  });
}
function button(parent, text, fn, kind = 'secondary') {
  const item = document.createElement('button'); item.textContent = text; item.className = kind; bind(item, fn); parent.append(item);
}
function showEvidence(value, summary) {
  pendingControl = null;
  $('detail').textContent = JSON.stringify(value, null, 2);
  $('action-status').textContent = summary;
  $('evidence').querySelector('details').open = true;
  $('evidence').scrollIntoView({block: 'nearest'});
}
async function inspect(kind, id) {
  pendingControl = null;
  const value = await api(kind + '/' + encodeURIComponent(id));
  $('detail').textContent = JSON.stringify(value, null, 2);
  $('action-status').textContent = kind === 'approval' ? 'Review the exact arguments below before allowing or denying this action.' : (value.task || 'Recorded details loaded.');
  $('evidence').querySelector('details').open = true;
  $('evidence').scrollIntoView({block: 'nearest'});
  selectedAction = kind === 'approval' ? value : null;
  $('action-review').hidden = !selectedAction;
  return value;
}
function paint(state) {
  window.dispatchEvent(new CustomEvent('hud:world-update', {detail: state.world}));
  latest = state; controlState();
  if (pendingControl && state.daemon.state === pendingControl.expected) {
    $('action-status').textContent = pendingControl.action + ' acknowledged: worker is ' + state.daemon.state + (state.daemon.execution_enabled ? '. Local dispatch enabled; approvals still apply.' : '. Task dispatch is not enabled.');
    pendingControl = null;
  }
  $('dnd').textContent = state.delivery.dnd_until > Date.now()/1000 ? 'DND active · extend 60m' : 'DND 60m';
  $('review-count').textContent = String(state.pending_actions_total);
  $('quiet').textContent = state.delivery.quiet ? 'Quiet: on — turn off' : 'Quiet: off — turn on';
  $('runtime-help').textContent = state.policy.error || (state.policy.runtime_enabled ? 'Local execution is enabled. Worker: ' + state.daemon.state + '. Tasks and tool actions still need their own approvals.' : 'Local execution is OFF. Starting the worker alone will not execute tasks. Open Local execution setup to configure an installed model.');
  if (!runtimeModelDirty && document.activeElement !== $('runtime-model')) $('runtime-model').value = state.policy.local_model || '';
  if (changed('policyLevel', state.policy.level) && document.activeElement !== $('level')) $('level').value = state.policy.level || 'L1';
  $('context-state').textContent = state.context.enabled === true ? 'CONTEXT OPT-IN / ON DEMAND' : state.context.enabled === false ? 'CONTEXT OFF' : 'CONTEXT UNKNOWN / REFUSED';
  $('privacy-state').textContent = (state.context.error || (state.context.enabled ? 'Context explicitly opted in; collection on demand' : 'Context collection OFF')) + ' · local state · no implicit model execution';
  $('observed').textContent = 'Observed ' + new Date(state.observed_at * 1000).toLocaleTimeString() + ' · ' + state.source;
  $('daemon').textContent = state.daemon.state + (state.daemon.execution_enabled ? ' / enabled' : ' / no dispatch');
  $('hardware').textContent = state.hardware.cpu_percent.toFixed(1) + '% / ' + state.hardware.memory_percent.toFixed(1) + '%';
  $('counts').textContent = state.jobs_total + ' jobs / ' + state.pending_actions_total + ' reviews';
  $('usage').textContent = (state.usage.day.actual_input_tokens || 0) + ' in · ' + (state.usage.day.actual_output_tokens || 0) + ' out / unknown';
  $('policy').textContent = state.policy.error || state.policy.level + ' · ' + state.policy.mode + ' · ' + state.policy.connectivity;
  $('job-count').textContent = 'Latest ' + state.jobs.length + ' of ' + state.jobs_total;
  if (changed('traces', state.traces.items)) {
  $('traces').replaceChildren();
  for (const trace of state.traces.items) {
    const item = document.createElement('div'); item.className = 'item';
    const label = document.createElement('p'); label.textContent = trace.id + ' · ' + trace.state + (trace.complete ? '' : ' · incomplete'); item.append(label);
    button(item, 'Inspect trace', async () => { showEvidence(await api('trace/' + encodeURIComponent(trace.id)), 'Recorded trace loaded; no actions executed.'); });
    button(item, 'Why', async () => { showEvidence(await api('trace/' + encodeURIComponent(trace.id) + '?operation=why'), 'Recorded explanation loaded; no actions executed.'); });
    button(item, 'Inspect step (no effects)', async () => {
      const step = prompt('Recorded step number (inspection only, no effects):', '0');
      if (step === null) return;
      if (!/^\d{1,4}$/.test(step)) throw Error('Enter a recorded step number.');
      showEvidence(await api('trace/' + encodeURIComponent(trace.id) + '?operation=replay&step=' + step), 'Recorded step inspection only; no actions executed.');
    });
    $('traces').append(item);
  }
  }
  $('evaluation').textContent = JSON.stringify(state.evaluation, null, 2);
  $('audit').textContent = JSON.stringify(state.audit, null, 2);
  $('budget').textContent = JSON.stringify({inbox: state.inbox, reservations: state.usage, delivery: state.delivery}, null, 2);
  if (changed('jobs', state.jobs)) {
  $('jobs').replaceChildren();
  for (const job of state.jobs) {
    const card = document.createElement('div'); card.className = 'item';
    const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = job.status + ' · ' + job.id + (job.active_role ? ' · ' + job.active_role : '');
    const task = document.createElement('p'); task.textContent = job.task; card.append(tag, task);
    button(card, 'Inspect', () => inspect('job', job.id));
    if (job.status === 'awaiting_approval') {
      button(card, 'Approve task', async () => {
        const full = await inspect('job', job.id);
        const phrase = 'APPROVE QUEUED TASK ' + job.id;
        const confirmation = prompt('Review the actual task:\n' + full.task + '\n\nQueue approval is NOT tool approval. Type: ' + phrase);
        if (confirmation !== null) await perform('review', {id: job.id, action: 'approve', confirmation});
      });
      button(card, 'Reject', () => perform('review', {id: job.id, action: 'reject'}));
    }
    if (['awaiting_approval', 'approved', 'running'].includes(job.status)) button(card, 'Cancel', () => perform('review', {id: job.id, action: 'cancel'}), 'danger');
    if (job.delegated && ['error', 'interrupted'].includes(job.status)) button(card, 'Resume for review', () => perform('resume', {id: job.id}));
    $('jobs').append(card);
  }
  }
  if (changed('approvals', state.pending_actions)) {
  $('approvals').replaceChildren();
  for (const review of state.pending_actions) {
    const card = document.createElement('div'); card.className = 'item';
    const label = document.createElement('p'); label.textContent = review.tool + ' · ' + review.id; card.append(label);
    button(card, 'Review exact action', () => inspect('approval', review.id)); $('approvals').append(card);
  }
  }
  if (changed('workflows', state.workflows)) {
  $('workflows').replaceChildren();
  for (const workflow of state.workflows) {
    const card = document.createElement('pre');
    card.textContent = workflow.id + ' · ' + workflow.status + '\n' + workflow.nodes.map(n => n.id + ' [' + n.status + '] ' + n.role + ' ← ' + (n.depends_on.join(', ') || 'root')).join('\n');
    $('workflows').append(card);
  }  }

}
async function connect(restore = false) {
  window.dispatchEvent(new Event('hud:disconnected'));
  generation++; const version = generation;
  $('connection').textContent = 'Connecting…';
  if (socket) socket.close();
  token = restore ? '' : $('token').value; $('token').value = '';
  authenticated = false;
  try {
    if (restore) {
      const check = await fetch('/api/session');
      if (!check.ok) throw Error('Connect with your private HUD token.');
      if (version !== generation) return;
      authenticated = true;
    }
    if (!restore && $('remember-browser').checked) await api('session', {});
    const state = await api('state');
    if (version !== generation) return;
    authenticated = true; access(true); paint(state);
  }
  catch (error) { if (version !== generation) return; authenticated = false; access(false); token = ''; $('connection').textContent = 'Disconnected · authentication failed'; throw error; }
  if (version !== generation) return;
  window.dispatchEvent(new CustomEvent('hud:connected', {detail: {api}}));
  socket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
  socket.onopen = () => { if (version === generation) socket.send(JSON.stringify({token})); };
  socket.onmessage = event => { if (version === generation) { paint(JSON.parse(event.data)); $('connection').textContent = 'LIVE · authenticated'; } };
  socket.onclose = () => { if (version === generation) $('connection').textContent = 'Disconnected · reconnect to refresh'; };
  socket.onerror = () => { if (version === generation) message('Live connection failed. Reconnect; no fabricated updates.'); };
}
if ($('connect')) bind($('connect'), () => connect());
if ($('disconnect')) bind($('disconnect'), async () => {
  const headers = token ? {Authorization: 'Bearer ' + token} : {};
  authenticated = false; access(false);
  window.dispatchEvent(new Event('hud:disconnected'));
  generation++; if (socket) socket.close(); token = ''; selectedAction = null;
  $('action-review').hidden = true; $('connection').textContent = 'Disconnected';
  const response = await fetch('/api/session', {method: 'DELETE', headers, keepalive: true});
  if (!response.ok && response.status !== 401) throw Error('Could not forget browser; retry logout before closing.');
});

for (const control of document.querySelectorAll('[data-control]')) bind(control, async () => {
  const action = control.dataset.control;
  const confirmation = ['start', 'resume'].includes(action) ? prompt('This can dispatch previously approved work. Type: ' + action.toUpperCase() + ' JARVIS') : '';
  if (confirmation !== null) await perform('control', {action, confirmation});
});
bind($('allow'), async () => {
  if (!selectedAction) throw Error('Inspect a real pending action first.');
  const row = selectedAction;
  const confirmation = prompt('Read the exact arguments/diff shown in Evidence. Type: ' + row.confirmation);
  if (confirmation !== null) { await perform('decide', {id: row.id, allow: true, confirmation}); selectedAction = null; $('action-review').hidden = true; }
});
bind($('deny'), async () => {
  if (!selectedAction) throw Error('Inspect a real pending action first.');
  await perform('decide', {id: selectedAction.id, allow: false}); selectedAction = null; $('action-review').hidden = true;
});
bind($('autonomy'), async () => {
  const level = $('level').value, confirmation = prompt('Operator policy change; never bypasses action reviews. Type: SET AUTONOMY ' + level);
  if (confirmation !== null) await perform('autonomy', {level, confirmation});
});
bind($('quiet'), () => perform('delivery', {quiet: !latest?.delivery.quiet}));
bind($('dnd'), () => perform('delivery', {minutes: 60}));
bind($('dnd-off'), () => perform('delivery', {minutes: 0}));
$('runtime-model').addEventListener('input', () => { runtimeModelDirty = true; });
for (const [id, enabled] of [['runtime-enable', true], ['runtime-disable', false]]) bind($(id), async () => {
  const phrase = enabled ? 'ENABLE LOCAL RUNTIME' : 'DISABLE LOCAL RUNTIME';
  const model = $('runtime-model').value.trim();
  if (enabled && (!/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$/.test(model) || model.toLowerCase().endsWith(':cloud'))) throw Error('Enter an installed local Ollama model name, not a cloud alias.');
  const confirmation = prompt('Change local execution policy only. Existing task and tool approvals remain required. Type: ' + phrase);
  if (confirmation !== null) { await perform('runtime', {enabled, local_model: model, confirmation}); runtimeModelDirty = false; }
});
$('delegate').addEventListener('submit', async event => {
  event.preventDefault(); const submit = event.submitter; submit.disabled = true; message('');
  try { await perform('create', {task: $('task').value, max_seconds: Number($('seconds').value)}); $('task').value = ''; }
  catch (error) { message(error.message); } finally { submit.disabled = false; }
});
window.addEventListener('hud:world-ready', () => { if (authenticated && !window.nexusGui) window.dispatchEvent(new CustomEvent('hud:connected', {detail: {api}})); });

access(false);
if (window.nexusGui) {
  let refreshing = false;
  async function refresh() {
    if (refreshing || document.hidden || window.nexusGui.view !== 'operations') return;
    refreshing = true;
    try { await window.nexusGui.ready; const state = await api('state'); authenticated=true; access(true); paint(state); $('connection').textContent='Connected · same Nexus GUI'; }
    catch (e) {message(e.message); $('connection').textContent='Operations unavailable';}
    finally {refreshing=false;}
  }
  window.addEventListener('nexus:view',refresh);
  window.nexusGui.ready.then(refresh);
  setInterval(refresh,2000);
} else connect(true).catch(() => { $('connection').textContent = 'Disconnected'; });

})();
