/** Real headless browser, scripted test-only APIs; never generates code or calls a model. */
import {test, before, after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import bundledChromium from '@sparticuz/chromium';

let browser;
const base = process.env.NEXUS_TEST_URL || 'http://127.0.0.1:8787';
before(async () => {
  browser = await chromium.launch({executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || await bundledChromium.executablePath(), args: bundledChromium.args.filter(a => a !== "--single-process"), headless: true});
});
after(async () => { await browser?.close(); });

async function setup({demo = false, restore = false, width = 1568, height = 770} = {}) {
  const context = await browser.newContext({viewport: {width, height}});
  const page = await context.newPage();
  const errors = [];
  const requests = [];
  const stats = {demo, workspace: 'C:\\Nexus', roles: 1, version: 'test', agents: {
    local: {model: 'test-model:8b', provider: 'openai-compatible', tool_mode: 'text'}
  }, keys: {}, pipelines: {check: {steps: ['general']}, build: {steps: ['general']}}, settings: {max_turns: 24}};
  let failApproval = false, failCancel = false, failRecall = false;
  page.on('pageerror', e => errors.push(e.message));
  await page.addInitScript(({restore}) => {
    window.matchMedia = () => ({matches: true});
    window.CSS.escape ||= v => v;
    window._streams = [];
    window._intervals = [];
    window.setInterval = (fn, ms) => { window._intervals.push({fn, ms}); return window._intervals.length; };
    window.EventSource = class {
      static CLOSED = 2;
      constructor(url) { this.url = url; window._streams.push(this); }
      close() { this.readyState = 2; }
    };
    window.sendEvent = event => window._streams.at(-1).onmessage({data: JSON.stringify(event)});
    if (restore) sessionStorage.setItem('nexus.activeTask', 'test-task');
  }, {restore});
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const payload = route.request().postDataJSON();
    requests.push({path, payload});
    let status = 200, body = {};
    if (path === '/api/stats') body = stats;
    else if (path === '/api/roles') body = [{name: 'general', category: 'general', agent: 'local'}];
    else if (path === '/api/ollama') body = {connected: true, models: ['test-model:8b'], url: 'http://localhost:11434'};
    else if (path === '/api/github') body = {connected: false, message: 'Test fixture: not connected'};
    else if (path === '/api/task') { status = 202; body = {task_id: 'test-task'}; }
    else if (path === '/api/task/test-task') body = {task_id: 'test-task', task: 'Create hello.txt', created: Date.now()/1000, status: 'awaiting_approval'};
    else if (path.startsWith('/api/approve/')) { status = failApproval ? 503 : 200; body = failApproval ? {error: 'Approval not received'} : {ok: true}; }
    else if (path.startsWith('/api/cancel/')) { status = failCancel ? 503 : 200; body = failCancel ? {error: 'Stop failed'} : {ok: true}; }
    else if (path === '/api/recall') { status = failRecall ? 503 : 200; body = failRecall ? {error: 'Recall unavailable'} : []; }
    else if (path.startsWith('/api/memory/')) body = {text: '', enabled: true};
    else if (path === '/api/brain') body = {skills: {}, sessions: {}, memory: {}, learning: {enabled: false}};
    await route.fulfill({status, json: body});
  });
  await page.goto(base + '/dashboard');
  await page.waitForFunction(() => document.querySelector('#liveNotice').textContent !== 'Connecting to live server…');
  return {page, context, errors, requests, setFailure: (which, value) => {if (which === 'approval') failApproval = value; if (which === 'cancel') failCancel = value; if (which === 'recall') failRecall = value;}};
}

async function start(page) {
  await page.fill('#taskInput', 'Create hello.txt containing Hello Nexus');
  await page.click('#runBtn');
  await page.waitForFunction(() => window._streams.length > 0);
}
async function approval(page, event_id = 1) {
  await page.evaluate(event_id => window.sendEvent({type: 'approval_request', event_id, approval_id: 'approval-1', tool: 'write_file', risk: 'write', preview: 'hello.txt (11 characters)'}), event_id);
}

test('approval controls stay visible and click failures keep the request retryable', async () => {
  const {page, context, errors, requests, setFailure} = await setup();
  try {
    await start(page);
    await approval(page);
    for (const id of ['apYes', 'apAlways', 'apNo']) {
      const box = await page.locator('#' + id).boundingBox();
      assert.ok(box && box.width > 50 && box.height >= 30 && box.y >= 0 && box.y + box.height <= 770, id);
    }
    setFailure('approval', true);
    await page.click('#apYes');
    await page.waitForFunction(() => document.querySelector('#actionError').textContent.includes('not received'));
    assert.equal(await page.locator('#approvalCard').isVisible(), true);
    assert.equal(await page.locator('#apYes').isEnabled(), true);
    setFailure('approval', false);
    await page.click('#apAlways');
    await page.waitForFunction(() => document.querySelector('#approvalCard').classList.contains('hidden'));
    assert.deepEqual(requests.filter(r => r.path.startsWith('/api/approve/')).at(-1).payload,
      {approved: true, scope: 'session', approval_id: 'approval-1'});
    assert.deepEqual(errors, []);
  } finally {await context.close();}
});

test('side cards do not collapse, pipeline survives stats refresh, recall errors are shown', async () => {
  const {page, context, errors, setFailure} = await setup();
  try {
    for (const id of ['telTools', 'brainQuery', 'refreshLocal', 'refreshGithub']) {
      const box = await page.locator('#' + id).boundingBox();
      assert.ok(box?.height > 0, id);
    }
    await page.selectOption('#pipelineSelect', 'check');
    await page.evaluate(() => window._intervals.find(t => t.ms === 15000).fn());
    assert.equal(await page.inputValue('#pipelineSelect'), 'check');
    await page.locator('[data-pipe="build"]').click();
    assert.equal(await page.inputValue('#pipelineSelect'), 'build');
    setFailure('recall', true);
    await page.locator('#brainGo').click();
    await page.waitForFunction(() => document.querySelector('#brainList').textContent.includes('Recall unavailable'));
    assert.deepEqual(errors, []);
  } finally {await context.close();}
});

test('clear cannot erase an active run; stop errors retry; duplicate events ignored', async () => {
  const {page, context, setFailure, errors} = await setup();
  try {
    await start(page);
    assert.equal(await page.locator('#clearBtn').isDisabled(), true);
    await approval(page);
    await approval(page);
    assert.equal(await page.locator('#stream .step').count(), 2); // task + one approval
    setFailure('cancel', true);
    await page.click('#cancelBtn');
    await page.waitForFunction(() => document.querySelector('#actionError').textContent.includes('Stop failed'));
    assert.equal(await page.locator('#cancelBtn').isEnabled(), true);
    setFailure('cancel', false);
    await page.click('#cancelBtn');
    await page.evaluate(() => window.sendEvent({type: 'done', event_id: 2, status: 'cancelled', result: {ok: false}}));
    assert.equal(await page.locator('#clearBtn').isEnabled(), true);
    assert.equal(await page.locator('#phaseText').textContent().then(s => s.includes('cancelled')), true);
    assert.deepEqual(errors, []);
  } finally {await context.close();}
});

test('live dashboard refuses demo mode; no fabricated globe data', async () => {
  const {page, context, errors} = await setup({demo: true});
  try {
    assert.equal(await page.locator('#runBtn').isDisabled(), true);
    assert.equal(await page.locator('#globe').count(), 0);
    assert.equal(await page.locator('#statRegions').textContent(), 'Unavailable');
    assert.match(await page.locator('#liveNotice').textContent(), /disabled/);
    assert.deepEqual(errors, []);
  } finally {await context.close();}
});

test('refresh restores active task and approval; narrow screen actions remain accessible', async () => {
  const {page, context, errors} = await setup({restore: true, width: 390, height: 844});
  try {
    await page.waitForFunction(() => window._streams.length > 0);
    await approval(page);
    await page.locator('#apNo').scrollIntoViewIfNeeded();
    await page.click('#apNo');
    await page.waitForFunction(() => document.querySelector('#approvalCard').classList.contains('hidden'));
    assert.deepEqual(errors, []);
  } finally {await context.close();}
});

test('late approval HTTP acknowledgement cannot hide the next pending request', async () => {
  const {page, context, errors} = await setup();
  try {
    await start(page);
    await approval(page);
    await page.route('**/api/approve/**', async route => {
      await page.evaluate(() => {
        window.sendEvent({type: 'approval_response', event_id: 2, approved: true});
        window.sendEvent({type: 'approval_request', event_id: 3, approval_id: 'approval-2', tool: 'write_file', preview: 'second.txt'});
      });
      await route.fulfill({json: {ok: true}});
    });
    await page.click('#apYes');
    await page.waitForFunction(() => document.querySelector('#apPreview').textContent === 'second.txt');
    assert.equal(await page.locator('#approvalCard').isVisible(), true);
    assert.equal(await page.locator('#apYes').isEnabled(), true);
    assert.deepEqual(errors, []);
  } finally {await context.close();}
});

test('model picker, refresh, filters, map selection and reset are wired to real actions', async () => {
  const {page, context, errors, requests} = await setup();
  try {
    await page.selectOption('#localModel', 'test-model:8b');
    assert.equal(await page.locator('#metaRouting').textContent(), 'selected local');
    await page.locator('#refreshLocal').click();
    assert.equal(await page.inputValue('#localModel'), 'test-model:8b');
    const githubChecks = requests.filter(r => r.path === '/api/github').length;
    await page.locator('#refreshGithub').click();
    assert.ok(requests.filter(r => r.path === '/api/github').length > githubChecks);
    await page.locator('#map .node').first().focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('#ndName').textContent(), 'local');
    await page.locator('#resetMap').click();
    await page.locator('label.toggle').filter({has: page.locator('#readOnly')}).click();
    await page.locator('label.toggle').filter({has: page.locator('#autoYes')}).click();
    await start(page);
    const payload = requests.find(r => r.path === '/api/task').payload;
    assert.equal(payload.local_model, 'test-model:8b');
    assert.equal(payload.read_only, true);
    assert.equal(payload.auto_yes, true);
    await page.evaluate(() => window.sendEvent({type: 'think', event_id: 1, agent: 'local', model: 'test-model:8b', turn: 1}));
    await page.locator('[data-f="tools"]').click();
    assert.ok(await page.locator('#stream .step.hidden-filter').count() > 0);
    await page.locator('[data-f="all"]').click();
    assert.equal(await page.locator('#stream .step.hidden-filter').count(), 0);
    assert.deepEqual(errors, []);
  } finally {await context.close();}
});

test('large code previews and short windows keep approval and stop reachable', async () => {
  const {page, context, errors} = await setup({width: 1366, height: 640});
  try {
    await start(page);
    await page.evaluate(() => window.sendEvent({type: 'approval_request', event_id: 1, approval_id: 'large-preview', tool: 'write_file', preview: 'hello.txt\n' + 'Hello Nexus\n'.repeat(100)}));
    await page.locator('#apNo').scrollIntoViewIfNeeded();
    await page.click('#apNo');
    await page.waitForFunction(() => document.querySelector('#approvalCard').classList.contains('hidden'));
    await page.locator('#cancelBtn').click();
    assert.deepEqual(errors, []);
  } finally {await context.close();}
});
