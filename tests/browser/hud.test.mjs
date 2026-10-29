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
  browser = await chromium.launch({executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || await bundledChromium.executablePath(), args: bundledChromium.args.filter(a => a !== '--single-process'), headless: true});
});
after(async () => { await browser?.close(); });
async function connected(width = 1400) {
  const context = await browser.newContext({viewport: {width, height: 1000}});
  const page = await context.newPage();
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  await page.goto(base);
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
  assert.match(await page.locator('#evaluation').innerText(), /existing sessions.db/);
  assert.match(await page.locator('#evaluation').innerText(), /"advisory_only": true/);
  assert.match(await page.locator('#evaluation').innerText(), /"actual_cost_usd": null/);
  assert.equal(await page.locator('#token').inputValue(), '');
  assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
  assert.equal(new URL(page.url()).search, '');
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
  await page.getByRole('button', {name: 'Disconnect', exact: true}).click();
  assert.equal(await page.locator('#connection').innerText(), 'Disconnected');
  await page.locator('#task').fill('Must not submit disconnected');
  await page.getByRole('button', {name: 'Queue task', exact: true}).click();
  assert.match(await page.locator('#error').innerText(), /Connect/);
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
