'use strict';
const $ = id => document.getElementById(id);
let token = '', socket = null, generation = 0, selectedAction = null, latest = null;
const message = text => { $('error').textContent = text; };
async function api(path, body) {
  if (!token) throw Error('Connect using your private HUD token first.');
  const response = await fetch('/api/' + path, {method: body === undefined ? 'GET' : 'POST', headers: {Authorization: 'Bearer ' + token, 'Content-Type': 'application/json'}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
  const value = await response.json();
  if (!response.ok) throw Error(value.detail || value.error || 'Request refused. Inspect state and retry.');
  return value;
}
async function perform(operation, body) {
  const value = await api('action/' + operation, body);
  $('detail').textContent = JSON.stringify(value, null, 2);
  paint(await api('state'));
  return value;
}
function bind(button, fn) {
  button.addEventListener('click', async () => {
    button.disabled = true; message('');
    try { await fn(); } catch (error) { message(error.message); }
    finally { button.disabled = false; }
  });
}
function button(parent, text, fn, kind = 'secondary') {
  const item = document.createElement('button'); item.textContent = text; item.className = kind; bind(item, fn); parent.append(item);
}
async function inspect(kind, id) {
  const value = await api(kind + '/' + encodeURIComponent(id));
  $('detail').textContent = JSON.stringify(value, null, 2);
  selectedAction = kind === 'approval' ? value : null;
  $('action-review').hidden = !selectedAction;
  return value;
}
function paint(state) {
  latest = state;
  $('context-state').textContent = state.context.enabled === true ? 'CONTEXT OPT-IN / ON DEMAND' : state.context.enabled === false ? 'CONTEXT OFF' : 'CONTEXT UNKNOWN / REFUSED';
  $('privacy-state').textContent = (state.context.error || (state.context.enabled ? 'Context explicitly opted in; collection on demand' : 'Context collection OFF')) + ' · local state · no implicit model execution';
  $('observed').textContent = 'Observed ' + new Date(state.observed_at * 1000).toLocaleTimeString() + ' · ' + state.source;
  $('daemon').textContent = state.daemon.state + (state.daemon.execution_enabled ? ' / enabled' : ' / no dispatch');
  $('hardware').textContent = state.hardware.cpu_percent.toFixed(1) + '% / ' + state.hardware.memory_percent.toFixed(1) + '%';
  $('counts').textContent = state.jobs_total + ' jobs / ' + state.pending_actions_total + ' reviews';
  $('usage').textContent = (state.usage.day.actual_input_tokens || 0) + ' in · ' + (state.usage.day.actual_output_tokens || 0) + ' out / unknown';
  $('policy').textContent = state.policy.error || state.policy.level + ' · ' + state.policy.mode + ' · ' + state.policy.connectivity;
  $('job-count').textContent = 'Latest ' + state.jobs.length + ' of ' + state.jobs_total;
  $('traces').replaceChildren();
  for (const trace of state.traces.items) {
    const item = document.createElement('div'); item.className = 'item';
    const label = document.createElement('p'); label.textContent = trace.id + ' · ' + trace.state + (trace.complete ? '' : ' · incomplete'); item.append(label);
    button(item, 'Inspect trace', async () => { $('detail').textContent = JSON.stringify(await api('trace/' + encodeURIComponent(trace.id)), null, 2); });
    button(item, 'Why', async () => { $('detail').textContent = JSON.stringify(await api('trace/' + encodeURIComponent(trace.id) + '?operation=why'), null, 2); });
    button(item, 'Inspect step (no effects)', async () => {
      const step = prompt('Recorded step number (inspection only, no effects):', '0');
      if (step === null) return;
      if (!/^\d{1,4}$/.test(step)) throw Error('Enter a recorded step number.');
      $('detail').textContent = JSON.stringify(await api('trace/' + encodeURIComponent(trace.id) + '?operation=replay&step=' + step), null, 2);
    });
    $('traces').append(item);
  }
  $('evaluation').textContent = JSON.stringify(state.evaluation, null, 2);
  $('audit').textContent = JSON.stringify(state.audit, null, 2);
  $('budget').textContent = JSON.stringify({inbox: state.inbox, reservations: state.usage, delivery: state.delivery}, null, 2);
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
  $('approvals').replaceChildren();
  for (const review of state.pending_actions) {
    const card = document.createElement('div'); card.className = 'item';
    const label = document.createElement('p'); label.textContent = review.tool + ' · ' + review.id; card.append(label);
    button(card, 'Review exact action', () => inspect('approval', review.id)); $('approvals').append(card);
  }
  $('workflows').replaceChildren();
  for (const workflow of state.workflows) {
    const card = document.createElement('pre');
    card.textContent = workflow.id + ' · ' + workflow.status + '\n' + workflow.nodes.map(n => n.id + ' [' + n.status + '] ' + n.role + ' ← ' + (n.depends_on.join(', ') || 'root')).join('\n');
    $('workflows').append(card);
  }
}
bind($('connect'), async () => {
  generation++; const version = generation;
  $('connection').textContent = 'Connecting…';
  if (socket) socket.close();
  token = $('token').value; $('token').value = '';
  try { paint(await api('state')); }
  catch (error) { token = ''; $('connection').textContent = 'Disconnected · authentication failed'; throw error; }
  socket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
  socket.onopen = () => { if (version === generation) socket.send(JSON.stringify({token})); };
  socket.onmessage = event => { if (version === generation) { paint(JSON.parse(event.data)); $('connection').textContent = 'LIVE · authenticated'; } };
  socket.onclose = () => { if (version === generation) $('connection').textContent = 'Disconnected · reconnect to refresh'; };
  socket.onerror = () => { if (version === generation) message('Live connection failed. Reconnect; no fabricated updates.'); };
});
bind($('disconnect'), async () => { generation++; if (socket) socket.close(); token = ''; selectedAction = null; $('action-review').hidden = true; $('connection').textContent = 'Disconnected'; });
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
$('delegate').addEventListener('submit', async event => {
  event.preventDefault(); const submit = event.submitter; submit.disabled = true; message('');
  try { await perform('create', {task: $('task').value, max_seconds: Number($('seconds').value)}); $('task').value = ''; }
  catch (error) { message(error.message); } finally { submit.disabled = false; }
});
