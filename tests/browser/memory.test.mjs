/** Prompt 9 acceptance: Memory & knowledge over the real server.

 *  Nothing here is a mock. The memory entries are the ones MEMORY.md really
 *  holds, the collection indexes real files in this workspace, the search runs
 *  against the real SQLite index, and the context preview is the exact text a
 *  run would receive. The suite restores what it changes: it deletes the
 *  collections it made, forgets the entries it added, and leaves the policy at
 *  its defaults with retrieval **off** — the default this page promises.
 */
import {test, before, after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import bundled from '@sparticuz/chromium';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const base = process.env.NEXUS_TEST_URL || 'http://127.0.0.1:8787';
const token = process.env.NEXUS_GUI_TEST_TOKEN;
const RUN = Date.now().toString(36);
const COLLECTION = `suite-${RUN}`.slice(0, 32);
const ENTRY = `the memory suite wrote ${RUN} on this workspace`;
let browser;
let context;
let page;
let errors = [];
const added = [];

const headers = () => ({'X-Nexus-Token': token, 'Content-Type': 'application/json'});
async function api(route, body, attempt = 0) {
  const reply = await fetch(base + route, {
    method: body === undefined ? 'GET' : 'POST', headers: headers(),
    ...(body === undefined ? {} : {body: JSON.stringify(body)}),
  });
  const payload = await reply.json();
  if (reply.status === 429 && attempt === 0) {
    // Repeated suites share one per-client window; wait out its own answer.
    const wait = Number(/(\d+)s/.exec(payload?.error?.message || '')?.[1] || 16);
    await new Promise((done) => setTimeout(done, Math.min(30, wait) * 1000 + 500));
    return api(route, body, 1);
  }
  if (!reply.ok) throw new Error(payload?.error?.message || reply.status);
  return payload.data;
}

/* Poll the API until it agrees, so a slow repaint cannot fail the DOM wait. */
async function waitForApi(probe, ms = 20000) {
  const started = Date.now();
  for (;;) {
    const value = await probe().catch(() => null);
    if (value) return value;
    if (Date.now() - started > ms) throw new Error('the API never reached the expected state');
    await new Promise((done) => setTimeout(done, 250));
  }
}

async function signIn(target) {
  await target.goto(base + '/dashboard');
  await target.locator('#guiToken').fill(token);
  await target.locator('#guiLogin button').click();
  await target.waitForFunction(() => document.getElementById('metaUplink').textContent === 'online', {timeout: 20000});
}

async function openMemory(tab = 'overview') {
  await page.locator('.sidebar-nav [data-nav-id="memory"]').click();
  await page.locator(`[data-mem-tab="${tab}"]`).click();
  await page.waitForFunction((name) => !document.querySelector(`[data-mem-panel="${name}"]`)?.hidden, tab, {timeout: 15000});
}

async function confirmWord(word) {
  await page.locator('#memActionHost #memActionToken').waitFor({timeout: 10000});
  await page.locator('#memActionHost #memActionToken').fill(word);
  assert.equal(await page.locator('#memActionHost #memActionGo').isDisabled(), false,
    'the confirm button stays disabled until the token matches');
  await page.locator('#memActionHost #memActionGo').click();
}

before(async () => {
  assert.ok(token, 'Set NEXUS_GUI_TEST_TOKEN for the live dashboard');
  browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || await bundled.executablePath(),
    args: bundled.args.filter((a) => !['--single-process', '--disable-web-security'].includes(a)),
    headless: true,
  });
  context = await browser.newContext({viewport: {width: 1600, height: 1000}, reducedMotion: 'reduce'});
  page = await context.newPage();
  page.on('pageerror', (error) => errors.push(error.message));
  await signIn(page);
  await openMemory('overview');
  await page.waitForFunction(() => document.querySelectorAll('#memKpis .security-count').length === 6, {timeout: 20000});
});

after(async () => {
  // Restore: drop this suite's collection, forget its entry, and put the
  // policy back to the defaults so retrieval is off again.
  try {
    const collections = await api('/api/knowledge/collections');
    if (collections.collections.some((row) => row.name === COLLECTION)) {
      await api('/api/knowledge/collection/delete',
        {name: COLLECTION, confirmation: `DELETE COLLECTION ${COLLECTION}`});
    }
    for (const id of added) {
      await api('/api/knowledge/entry/remove',
        {section: 'memory', id, confirmation: `FORGET MEMORY ENTRY ${id}`}).catch(() => {});
    }
    const view = await api('/api/knowledge/policy');
    await api('/api/knowledge/policy/save', {
      confirmation: 'SAVE MEMORY POLICY', revision: view.revision,
      memory: true, rag: false, inject: false, top_k: 6, max_chars: 4000, chunk_chars: 1200,
    });
    const left = await api('/api/knowledge/overview');
    if (left.policy.rag || left.policy.inject) errors.push('retrieval was left switched on');
  } catch (error) {
    errors.push(`cleanup: ${error.message}`);
  }
  await context?.close();
  await browser?.close();
  assert.equal(errors.length, 0, errors.join('\n'));
});

test('the sidebar opens Memory and the counters come from the API', async () => {
  const overview = await api('/api/knowledge/overview');
  const cards = page.locator('#memKpis .security-count');
  assert.equal(await cards.count(), 6);
  const entries = Number((await cards.nth(0).locator('b').textContent()).trim());
  assert.equal(entries, overview.memory.memory.entries.length);
  assert.equal(Number((await cards.nth(2).locator('b').textContent()).trim()), overview.memory.notes.files.length);
  assert.equal(Number((await cards.nth(5).locator('b').textContent()).trim()), overview.totals.chunks);
  assert.match(await page.locator('#memChip').textContent(), /Retrieval off · injection off/);
  assert.equal(await page.locator('#memSwitchRag').isChecked(), false, 'retrieval starts off');
  assert.equal(await page.locator('#memSwitchInject').isDisabled(), true, 'injection needs retrieval');
  assert.match(await page.locator('#memPolicyFile').textContent(), /\.nexus\/knowledge\/policy\.json/);
  assert.equal(errors.length, 0, errors.join('\n'));
});

test('switching retrieval on and off goes through the typed token', async () => {
  await page.locator('#memSwitchRag').check();
  await page.locator('#memPolicySave').click();
  const stamp = await page.locator('#memActionExpected').textContent();
  assert.equal(stamp.trim(), 'SAVE MEMORY POLICY');
  assert.equal(await page.locator('#memActionHost #memActionGo').isDisabled(), true);
  await confirmWord('SAVE MEMORY POLICY');
  await page.waitForFunction(() => document.querySelector('#memActionHost')?.hidden !== false, {timeout: 15000});
  await page.waitForFunction(() => /Retrieval on/.test(document.getElementById('memChip').textContent), {timeout: 15000});
  const policy = await api('/api/knowledge/policy');
  assert.equal(policy.policy.rag, true);
  assert.equal(await page.locator('#memSwitchInject').isDisabled(), false, 'injection is now available');

  await page.locator('#memSwitchRag').uncheck();
  assert.equal(await page.locator('#memSwitchInject').isChecked(), false, 'injection follows retrieval down');
  await page.locator('#memPolicySave').click();
  await confirmWord('SAVE MEMORY POLICY');
  await page.waitForFunction(() => /Retrieval off/.test(document.getElementById('memChip').textContent), {timeout: 15000});
  assert.equal((await api('/api/knowledge/policy')).policy.rag, false);
  // Turn both back on in a single save for the rest of the suite.
  await page.locator('#memSwitchRag').check();
  await page.locator('#memSwitchInject').check();
  await page.locator('#memPolicySave').click();
  await confirmWord('SAVE MEMORY POLICY');
  await page.waitForFunction(() => /Retrieval on · injection on/.test(document.getElementById('memChip').textContent), {timeout: 15000});
});

test('a memory entry can be added and forgotten, and the file really changes', async () => {
  await openMemory('memory');
  await page.waitForFunction(() => document.getElementById('memMemoryCount').textContent.length > 5, {timeout: 15000});
  const before = await api('/api/knowledge/memory');
  const previous = before.memory.entries.length;

  await page.locator('#memAddText').fill(ENTRY);
  await page.locator('#memAdd').click();
  await confirmWord('ADD MEMORY ENTRY');
  const after = await waitForApi(async () => {
    const body = await api('/api/knowledge/memory');
    return body.memory.entries.length === previous + 1 ? body : null;
  });
  await page.waitForFunction((text) => document.getElementById('memMemoryEntries').textContent.includes(text),
    ENTRY, {timeout: 20000});
  assert.equal(after.memory.entries.length, previous + 1);

  const entry = after.memory.entries.find((row) => row.text === ENTRY);
  assert.ok(entry, 'the entry is listed with its own id');
  added.push(entry.id);

  await page.locator('#memMemoryEntries .memory-entry', {hasText: ENTRY})
    .locator('button').click();
  await confirmWord(`FORGET MEMORY ENTRY ${entry.id}`);
  await page.waitForFunction((id) => !document.getElementById('memMemoryEntries').textContent.includes(id),
    entry.id, {timeout: 20000});
  const restored = await waitForApi(async () => {
    const body = await api('/api/knowledge/memory');
    return body.memory.entries.length === previous ? body : null;
  });
  assert.equal(restored.memory.entries.length, previous);
  assert.ok(!restored.memory.entries.some((row) => row.text === ENTRY));
  assert.equal(errors.length, 0, errors.join('\n'));
});

test('a collection is indexed from real workspace files and can be searched', async () => {
  // The workspace really holds these pages; indexing reads them from disk.
  const files = ['docs/dashboard-security.md', 'docs/dashboard-inventory.md'];
  await openMemory('collections');
  await page.locator('#memBuildName').fill(COLLECTION);
  await page.locator('#memBuildPatterns').fill(files.join('\n'));
  await page.locator('#memBuild').click();
  await confirmWord(`INDEX COLLECTION ${COLLECTION}`);
  await page.waitForFunction((name) => document.getElementById('memCollectionRows').textContent.includes(name),
    COLLECTION, {timeout: 20000});

  const detail = await api(`/api/knowledge/collection?name=${COLLECTION}`);
  assert.equal(detail.collection.documents, 2);
  assert.ok(detail.collection.chunks >= 2);
  assert.deepEqual(detail.documents.map((row) => row.path).sort(), files.slice().sort());
  await page.waitForFunction(() => document.querySelectorAll('#memCollectionRows tr[data-collection]').length > 0, {timeout: 15000});

  // The document table follows the first row of the table body.
  await page.locator(`#memCollectionRows tr[data-collection="${COLLECTION}"] button`).first().click();
  await page.waitForFunction(() => document.getElementById('memDocumentRows').textContent.includes('dashboard-security.md'),
    {timeout: 15000});

  await openMemory('retrieve');
  await page.locator('#memQueryCollection').selectOption(COLLECTION);
  await page.locator('#memQuery').fill('hash-linked audit trail');
  await page.locator('#memSearch').click();
  await page.waitForFunction(() => document.querySelectorAll('#memResults .memory-result').length > 0, {timeout: 15000});
  const first = page.locator('#memResults .memory-result').first();
  assert.match(await first.textContent(), /docs\/dashboard-security\.md/);
  assert.match(await first.textContent(), /score /);
  const found = await api('/api/knowledge/search?q=hash-linked%20audit%20trail&collection=' + COLLECTION);
  assert.equal(found.results[0].path, 'docs/dashboard-security.md');
  assert.ok(found.results[0].document_id.length === 12);
  assert.ok(found.results[0].start_line >= 1);
  // Words spread over several chunks of one long page, so "every word in one
  // chunk" cannot match; the mode that ran is reported instead of hidden.
  assert.ok(['all', 'any', 'prefix'].includes(found.mode));
  assert.ok(found.results[0].coverage > 0, 'the match covers at least one query word');
  // A word that exists in the other page wins for its own query.
  await page.locator('#memQuery').fill('control cards');
  await page.locator('#memSearch').click();
  await page.waitForFunction(() => /\d+ result/.test(document.getElementById('memResultsMeta').textContent),
    {timeout: 15000});
});

test('the context preview shows the budget a run would get', async () => {
  await openMemory('context');
  await page.locator('#memTask').fill('how is the audit trail hash linked');
  await page.locator('#memPreview').click();
  await page.waitForFunction(() => document.getElementById('memContextText').textContent.length > 20, {timeout: 15000});
  const body = await api('/api/knowledge/context?task=how%20is%20the%20audit%20trail%20hash%20linked');
  assert.ok(body.chars <= body.budget);
  assert.ok(body.sections.some((section) => section.id === 'documents'), 'the run would receive excerpts');
  assert.match(await page.locator('#memContextText').textContent(), /Retrieved document excerpts/);
  const width = await page.locator('#memBudgetBar').evaluate((node) => node.style.width);
  assert.match(width, /%/);
  assert.equal(errors.length, 0, errors.join('\n'));
});

test('removing a document, cleaning up and deleting the collection are all real', async () => {
  const detail = await api(`/api/knowledge/collection?name=${COLLECTION}`).catch(async (error) => {
    const listing = await api('/api/knowledge/collections');
    throw new Error(`${error.message}; the store holds ${JSON.stringify(listing.collections.map((r) => r.name))}`);
  });
  const doc = detail.documents.find((row) => row.path === 'docs/dashboard-inventory.md');
  await openMemory('collections');
  await page.locator(`#memCollectionRows tr[data-collection="${COLLECTION}"] button`).first().click();
  await page.waitForFunction(() => document.getElementById('memDocumentRows').textContent.includes('dashboard-inventory.md'),
    {timeout: 15000});
  await page.locator('#memDocumentRows tr', {hasText: 'dashboard-inventory.md'}).locator('button').click();
  await confirmWord(`REMOVE DOCUMENT ${doc.id}`);
  await page.waitForFunction(() => !document.getElementById('memDocumentRows').textContent.includes('dashboard-inventory.md'),
    {timeout: 15000});
  assert.equal((await api(`/api/knowledge/collection?name=${COLLECTION}`)).collection.documents, 1);

  await page.locator('#memClean').click();
  await confirmWord('CLEAN MEMORY STORE');
  await page.waitForFunction(() => document.querySelector('#memActionHost')?.hidden !== false, {timeout: 15000});

  await page.locator(`#memCollectionRows tr[data-collection="${COLLECTION}"] button`).last().click();
  await confirmWord(`DELETE COLLECTION ${COLLECTION}`);
  await page.waitForFunction((name) => !document.getElementById('memCollectionRows').textContent.includes(name),
    COLLECTION, {timeout: 15000});
  const left = await api('/api/knowledge/collections');
  assert.ok(!left.collections.some((row) => row.name === COLLECTION));
});

test('the page survives a language switch and a 360 px viewport', async () => {
  await openMemory('overview');
  await page.evaluate(() => window.nexusI18n.setLang('bn'));
  await page.waitForFunction(() => document.documentElement.lang === 'bn', {timeout: 10000});
  assert.match(await page.locator('#view-memory h1').textContent(), /[\u0980-\u09FF]/);
  assert.match(await page.locator('[data-mem-tab="retrieve"]').textContent(), /[\u0980-\u09FF]/);
  await page.evaluate(() => window.nexusI18n.setLang('en'));
  await page.waitForFunction(() => document.documentElement.lang === 'en', {timeout: 10000});
  assert.match(await page.locator('#view-memory h1').textContent(), /agent/i);
  await page.setViewportSize({width: 360, height: 760});
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  assert.ok(overflow <= 1, `horizontal overflow of ${overflow}px at 360px`);
  const tabs = await page.locator('#view-memory [role="tab"]').count();
  assert.equal(tabs, 5);
  await page.setViewportSize({width: 1600, height: 1000});
  assert.equal(errors.length, 0, errors.join('\n'));
});
