/** Real HUD HTTP/WebSocket and browser; genuine temporary Studio jobs, no model calls. */
import {test, before, after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import bundledChromium from '@sparticuz/chromium';
const base = process.env.NEXUS_HUD_TEST_URL || 'http://127.0.0.1:8790';
const token = process.env.NEXUS_HUD_TEST_TOKEN;
let browser;
before(async () => {
  assert.ok(token, 'Set a dedicated fixture HUD token, never an actual account key');
  browser = await chromium.launch({executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || await bundledChromium.executablePath(), args: bundledChromium.args.filter(a => !['--single-process', '--disable-web-security'].includes(a)), headless: true});
});
after(async () => { await browser?.close(); });
async function connected(width = 1400) {
  const context = await browser.newContext({viewport: {width, height: 1000}});
  const page = await context.newPage();
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  await page.goto(base + '/?map=2d');
  await page.locator('#token').fill(token);
  await page.getByRole('button', {name: 'Connect', exact: true}).click();
  await page.waitForFunction(() => document.getElementById('connection').textContent.includes('LIVE'));
  return {page, context, errors};
}
test('actual live HUD auth, no storage/query token and no fake telemetry', async () => {
  const {page, context, errors} = await connected();
  assert.match(await page.locator('#hardware').innerText(), /% \/ .*%/);
  assert.match(await page.locator('#daemon').innerText(), /stopped \/ no dispatch/);
  assert.match(await page.locator('#usage').innerText(), /unknown/);
  assert.equal(await page.locator('#context-state').innerText(), 'CONTEXT OFF');
  assert.match(await page.locator('#privacy-state').innerText(), /Context collection OFF/);
  assert.match(await page.locator('#evaluation').textContent(), /existing sessions.db/);
  assert.match(await page.locator('#evaluation').textContent(), /"advisory_only": true/);
  assert.match(await page.locator('#evaluation').textContent(), /"actual_cost_usd": null/);
  assert.equal(await page.locator('#token').inputValue(), '');
  assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
  assert.equal(new URL(page.url()).searchParams.has('token'), false);
  assert.deepEqual(errors, []); await context.close();
});
test('real delegated job, wrong approval stays retryable, correct approval then cancellation', async () => {
  const {page, context, errors} = await connected();
  const task = 'HUD browser actual task ' + Date.now();
  await page.locator('#task').fill(task); await page.getByRole('button', {name: 'Queue task', exact: true}).click();
  const item = page.locator('#jobs .item').filter({hasText: task}); await item.waitFor();
  page.once('dialog', dialog => dialog.accept('wrong'));
  await item.getByRole('button', {name: 'Approve task', exact: true}).click();
  await page.waitForFunction(() => document.getElementById('error').textContent.includes('refused'));
  assert.equal(await item.getByRole('button', {name: 'Approve task', exact: true}).isEnabled(), true);
  page.once('dialog', dialog => dialog.accept(dialog.message().split('Type: ')[1]));
  await item.getByRole('button', {name: 'Approve task', exact: true}).click();
  await page.waitForFunction(t => [...document.querySelectorAll('#jobs .item')].find(e => e.textContent.includes(t))?.textContent.includes('approved'), task);
  await item.getByRole('button', {name: 'Cancel', exact: true}).click();
  await page.waitForFunction(t => [...document.querySelectorAll('#jobs .item')].find(e => e.textContent.includes(t))?.textContent.includes('cancelled'), task);
  assert.deepEqual(errors, []); await context.close();
});
test('stored untrusted task renders literally, never as HTML/script', async () => {
  const {page, context, errors} = await connected();
  const task = '<img src=x onerror="window.hudXss=true"> ' + Date.now();
  await page.locator('#task').fill(task); await page.getByRole('button', {name: 'Queue task', exact: true}).click();
  await page.locator('#jobs .item').filter({hasText: task}).waitFor();
  assert.equal(await page.locator('#jobs img').count(), 0);
  assert.equal(await page.evaluate(() => window.hudXss), undefined);
  assert.deepEqual(errors, []); await context.close();
});
test('mobile HUD fits viewport and disconnect does not silently reconnect', async () => {
  const {page, context, errors} = await connected(430);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.getByRole('button', {name: 'Logout / forget browser', exact: true}).click();
  assert.equal(await page.locator('#connection').innerText(), 'Disconnected');
  await page.locator('#task').fill('Must not submit disconnected');
  assert.equal(await page.getByRole('button', {name: 'Queue task', exact: true}).isDisabled(), true);
  assert.match(await page.locator('#connection-help').innerText(), /Connect/);
  assert.deepEqual(errors, []); await context.close();
});
test('failed reauthentication never leaves stale LIVE status', async () => {
  const {page, context, errors} = await connected();
  await page.locator('#token').fill('incorrect-private-token');
  await page.getByRole('button', {name: 'Connect', exact: true}).click();
  await page.waitForFunction(() => document.getElementById('connection').textContent.includes('authentication failed'));
  assert.equal((await page.locator('#connection').innerText()).includes('LIVE'), false);
  assert.deepEqual(errors, []); await context.close();
});

test('real observed trace inspect, why and step replay are authenticated GET-only no-effects views',
  {skip: !process.env.NEXUS_HUD_TEST_TRACE_ID}, async () => {
  const identifier = process.env.NEXUS_HUD_TEST_TRACE_ID;
  const {page, context, errors} = await connected();
  const item = page.locator('#traces .item').filter({hasText: identifier});
  await item.waitFor();
  const writes = [];
  page.on('request', request => { if (request.method() !== 'GET') writes.push(request.method()); });
  await item.getByRole('button', {name: 'Inspect trace', exact: true}).click();
  await page.waitForFunction(() => document.getElementById('detail').textContent.includes('tool_call'));
  const trace = JSON.parse(await page.locator('#detail').innerText());
  assert.equal(trace.id, identifier);
  assert.equal(await page.locator('#detail img').count(), 0);
  assert.equal(await page.evaluate(() => window.traceXss), undefined);
  await item.getByRole('button', {name: 'Why', exact: true}).click();
  await page.waitForFunction(() => document.getElementById('detail').textContent.includes('not hidden model reasoning'));
  const step = trace.events.find(event => event.kind === 'tool_call').step;
  page.once('dialog', dialog => dialog.accept(String(step)));
  await item.getByRole('button', {name: 'Inspect step (no effects)', exact: true}).click();
  await page.waitForFunction(() => document.getElementById('detail').textContent.includes('"effects": false'));
  assert.deepEqual(writes, []);
  assert.deepEqual(errors, []);
  await context.close();
});

test('trusted browser remembers reload and new context without exposing token; logout forgets', async () => {
  const context = await browser.newContext();
  let saved;
  try {
    const page = await context.newPage();
    await page.goto(base + '/?map=2d');
    await page.locator('#token').fill(token);
    await page.locator('#remember-browser').check();
    await page.getByRole('button', {name: 'Connect', exact: true}).click();
    await page.waitForFunction(() => document.getElementById('connection').textContent.includes('LIVE'));
    const cookies = await context.cookies();
    const session = cookies.find(c => c.name === 'nexus_hud_session');
    assert.ok(session?.httpOnly);
    assert.equal(session.sameSite, 'Strict');
    assert.ok(!session.value.includes(token));
    assert.ok(!(await page.evaluate(() => document.cookie)).includes('nexus_hud_session'));
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
    await page.reload();
    await page.waitForFunction(() => document.getElementById('connection').textContent.includes('LIVE'));
    assert.equal(await page.locator('#token').inputValue(), '');
    saved = await context.storageState();
  } finally { await context.close(); }
  const reopened = await browser.newContext({storageState: saved});
  try {
    const page = await reopened.newPage();
    await page.goto(base + '/?map=2d');
    await page.waitForFunction(() => document.getElementById('connection').textContent.includes('LIVE'));
    await page.getByRole('button', {name: 'Logout / forget browser', exact: true}).click();
    await page.waitForFunction(() => document.getElementById('connection').textContent === 'Disconnected');
    await page.reload();
    await page.waitForFunction(() => document.getElementById('connection').textContent === 'Disconnected');
    assert.equal((await reopened.cookies()).some(c => c.name === 'nexus_hud_session'), false);
    assert.equal((await page.request.get(base + '/api/session')).status(), 401);
  } finally { await reopened.close(); }
});

test('one dashboard prioritizes controls and scrolls over the map on desktop and mobile', async () => {
  for (const width of [1400, 430]) {
    const {page, context} = await connected(width);
    try {
      assert.ok(await page.evaluate(() => document.querySelector('#execution').getBoundingClientRect().top < document.querySelector('#world-widget').getBoundingClientRect().top));
      assert.equal(await page.locator('#diagnostics > details').getAttribute('open'), null);
      await page.getByRole('link', {name: 'World & weather', exact: true}).click();
      await page.locator('.world-stage').scrollIntoViewIfNeeded();
      const bounds = await page.locator('.world-stage').boundingBox();
      await page.mouse.move(bounds.x + bounds.width/2, bounds.y + Math.min(bounds.height/2,100));
      const before = await page.evaluate(() => scrollY);
      await page.mouse.wheel(0, 450);
      await page.waitForFunction(y => scrollY > y + 100, before);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.getByRole('link', {name: 'Controls', exact: true}).click();
      await page.waitForFunction(() => document.querySelector('#execution').getBoundingClientRect().top < 250);
      assert.equal(new URL(page.url()).pathname, '/');
    } finally { await context.close(); }
  }
});

test('runtime, quiet, DND and autonomy controls persist real policy with exact confirmations', async () => {
  const {page, context} = await connected();
  try {
    await page.locator('#execution summary').click();
    await page.locator('#runtime-model').fill('installed-test-model:latest');
    page.once('dialog', d => d.accept('wrong'));
    await page.locator('#runtime-enable').click();
    await page.waitForFunction(() => document.getElementById('error').textContent.length > 0);
    page.once('dialog', d => d.accept('ENABLE LOCAL RUNTIME'));
    await page.locator('#runtime-enable').click();
    await page.waitForFunction(() => document.getElementById('runtime-help').textContent.includes('is enabled'));
    page.once('dialog', d => d.accept('DISABLE LOCAL RUNTIME'));
    await page.locator('#runtime-disable').click();
    await page.waitForFunction(() => document.getElementById('runtime-help').textContent.includes('is OFF'));
    await page.locator('#quiet').click();
    await page.waitForFunction(() => document.getElementById('quiet').textContent.includes('Quiet: on'));
    await page.locator('#quiet').click();
    await page.waitForFunction(() => document.getElementById('quiet').textContent.includes('Quiet: off'));
    await page.locator('#dnd').click();
    await page.waitForFunction(() => document.getElementById('dnd').textContent.includes('DND active'));
    await page.locator('#dnd-off').click();
    await page.waitForFunction(() => document.getElementById('dnd').textContent === 'DND 60m');
    await page.locator('#level').selectOption('L0');
    page.once('dialog', d => d.accept('SET AUTONOMY L0'));
    await page.locator('#autonomy').click();
    await page.waitForFunction(() => document.getElementById('policy').textContent.startsWith('L0'));
    await page.locator('#level').selectOption('L1');
    page.once('dialog', d => d.accept('SET AUTONOMY L1'));
    await page.locator('#autonomy').click();
    await page.waitForFunction(() => document.getElementById('policy').textContent.startsWith('L1'));
  } finally { await context.close(); }
});

test('task buttons survive live snapshots instead of being replaced every refresh', async () => {
  const {page, context} = await connected();
  try {
    const task = 'Stable controls ' + Date.now();
    await page.locator('#task').fill(task);
    await page.getByRole('button', {name: 'Queue task', exact: true}).click();
    const item = page.locator('#jobs .item').filter({hasText: task});
    await item.waitFor();
    await item.getByRole('button', {name: 'Inspect', exact: true}).evaluate(n => { window.savedTaskButton = n; window.previousObservation = document.getElementById('observed').textContent; });
    await page.waitForFunction(() => document.getElementById('observed').textContent !== window.previousObservation);
    assert.equal(await page.evaluate(() => window.savedTaskButton.isConnected), true);
    await item.getByRole('button', {name: 'Inspect', exact: true}).click();
    await page.waitForFunction(() => document.getElementById('action-status').textContent.includes('Stable controls'));
    assert.match(await page.locator('#action-status').innerText(), /Stable controls/);
    await item.getByRole('button', {name: 'Cancel', exact: true}).click();
    await page.waitForFunction(t => [...document.querySelectorAll('#jobs .item')].find(n => n.textContent.includes(t))?.textContent.includes('cancelled'), task);
  } finally { await context.close(); }
});

test('Start, Pause, Resume, Stop and Kill reach real worker acknowledgements without inference', async () => {
  const {page, context} = await connected();
  page.setDefaultTimeout(25000);
  try {
    for (const [action, expected] of [['start','running'],['pause','paused'],['resume','running'],['stop','stopped'],['start','running'],['kill','killed']]) {
      if (['start','resume'].includes(action)) page.once('dialog', d => d.accept(action.toUpperCase() + ' JARVIS'));
      await page.locator(`[data-control="${action}"]`).click();
      try { await page.waitForFunction(state => document.getElementById('daemon').textContent.startsWith(state + ' /'), expected, {timeout:15000}); }
      catch { throw Error(action + ' failed: ' + await page.locator('#daemon').innerText() + ' / ' + await page.locator('#error').textContent() + ' / ' + await page.locator('#detail').textContent()); }
    }
    assert.match(await page.locator('#daemon').innerText(), /no dispatch/);
  } finally { await context.close(); }
});
