/** Prompt 5 acceptance: Computer Control is permissions-first, every mutation
 *  carries its own typed token, and the panic path latches and can be cleared.
 *
 *  The suite leaves the studio policy locked and the workspace as it found it. */
import {test, before, after} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {chromium} from 'playwright-core';
import bundled from '@sparticuz/chromium';

const base = process.env.NEXUS_TEST_URL || 'http://127.0.0.1:8787';
const token = process.env.NEXUS_GUI_TEST_TOKEN;
const scratch = 'tests/browser/.cc-suite.txt';
let browser;

async function resumeIfLatched() {
  const headers = {'X-Nexus-Token': token, 'Content-Type': 'application/json'};
  const state = await fetch(base + '/api/computer/state', {headers}).then((r) => r.json()).catch(() => null);
  if (!state?.data?.latch?.stopped) return;
  await fetch(base + '/api/computer/resume', {
    method: 'POST', headers, body: JSON.stringify({confirmation: 'RESUME COMPUTER', resume_queue: true}),
  });
}

before(async () => {
  assert.ok(token, 'Set NEXUS_GUI_TEST_TOKEN for the live dashboard');
  await resumeIfLatched();   // an earlier panic must not make every write fail
  browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || await bundled.executablePath(),
    args: bundled.args.filter((a) => !['--single-process', '--disable-web-security'].includes(a)),
    headless: true,
  });
});
after(async () => {
  await fs.rm(path.resolve('.cc-suite.txt'), {force: true}).catch(() => {});
  await browser?.close();
});

async function open(width = 1600, height = 1000) {
  const context = await browser.newContext({viewport: {width, height}, reducedMotion: 'reduce'});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(base + '/dashboard');
  await page.locator('#guiToken').fill(token);
  await page.locator('#guiLogin button').click();
  await page.waitForFunction(() => document.getElementById('metaUplink').textContent === 'online', {timeout: 20000});
  if (width > 900) await page.locator('.sidebar-nav [data-nav-id="computer"]').click();
  else await page.evaluate(() => window.nexusGui.show('computer'));
  await page.waitForSelector('#view-computer .computer-card', {timeout: 20000});
  await page.waitForFunction(() => document.getElementById('view-computer').dataset.busy === 'false', {timeout: 20000});
  return {page, context, errors};
}

const api = (page, route, body) => page.evaluate(
  async ([p, b]) => (await window.nexusGui.api(p, b)).data, [route, body]);

async function resetScratch() {
  await fs.rm(path.resolve('.cc-suite.txt'), {force: true}).catch(() => {});
}

async function writeScratch(page, text) {
  let revision = 'missing';
  try { revision = (await api(page, '/api/computer/file/preview', {path: scratch})).revision; } catch { /* new file */ }
  return api(page, '/api/computer/file/write', {
    path: scratch, text, revision, confirmation: `WRITE ${scratch}`,
  });
}

async function setLevels(page, files, browserLevel = 'off') {
  const state = await api(page, '/api/computer/state');
  return api(page, '/api/computer/settings', {
    profile: files === 'off' ? 'locked' : files === 'read' ? 'read-only' : 'review',
    levels: {files, browser: browserLevel},
    scopes: {roots: state.store.settings.scopes.roots, origins: state.store.settings.scopes.origins},
    revision: state.store.revision,
    confirmation: 'SAVE COMPUTER POLICY',
  });
}

async function openDir(page, dir) {
  await page.fill('#ccPath', dir);
  await page.locator('#ccOpen').click();
  await page.waitForFunction((value) => {
    const note = document.getElementById('ccTreeNote')?.textContent || '';
    return document.getElementById('ccPath').value === value && note.includes(value);
  }, dir, {timeout: 20000});
  await page.waitForFunction(() => document.querySelectorAll('#ccTree .computer-tree-row').length > 0,
    null, {timeout: 20000});
}

async function waitForTreeName(page, name) {
  await page.waitForFunction((wanted) => [...document.querySelectorAll('#ccTree .computer-tree-name')]
    .some((node) => node.textContent.includes(wanted)), name, {timeout: 20000});
}

async function reload(page) {
  await page.locator('#ccRefresh').click();
  await page.waitForFunction(() => document.getElementById('view-computer').dataset.busy === 'false');
}

test('the overview reads the real policy and never invents a capability', async () => {
  const {page, context, errors} = await open();
  try {
    const state = await api(page, '/api/computer/state');
    assert.equal(await page.locator('#ccCapabilities .computer-cap').count(), 7);
    const cards = await page.locator('#ccCapabilities .computer-cap').evaluateAll((nodes) => nodes.map((node) => ({
      id: node.dataset.cap, level: node.querySelector('.computer-chip').textContent,
    })));
    assert.deepEqual(cards.map((card) => card.id),
      ['files', 'browser', 'terminal', 'office', 'screen', 'apps', 'vscode']);
    const files = cards.find((card) => card.id === 'files');
    assert.equal(files.level.trim().toLowerCase(),
      state.capabilities.find((row) => row.id === 'files').level.toLowerCase());
    const screenText = await page.locator('[data-cap="screen"]').innerText();
    assert.match(screenText, /Unavailable|No display/);
    assert.match(await page.locator('#ccUpdated').innerText(), /^Read from the server at \d/);
    assert.equal(await page.locator('#ccPolicy dt').count() >= 5, true, 'policy facts come from permissions.yaml');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('permissions save only with the exact token and the current revision', async () => {
  const {page, context, errors} = await open();
  try {
    await setLevels(page, 'off');
    await reload(page);
    await page.locator('#ccTabPermissions').click();
    const before = await api(page, '/api/computer/state');
    assert.deepEqual(await page.locator('#ccLevel-files option').evaluateAll((nodes) => nodes.map((node) => node.value)),
      ['off', 'read', 'ask', 'auto'], 'the full trust ladder is offered');
    await page.locator('#ccLevel-files').selectOption('read');
    await page.locator('#ccSave').click();
    await page.waitForSelector('#ccConfirmRow:not([hidden])');
    assert.match(await page.locator('#ccConfirmLabel').innerText(), /SAVE COMPUTER POLICY/);
    await page.locator('#ccConfirm').fill('save computer policy');
    await page.locator('#ccConfirmGo').click();
    assert.equal((await api(page, '/api/computer/state')).store.revision, before.store.revision,
      'a wrong token changes nothing');

    await page.locator('#ccSave').click();
    await page.locator('#ccConfirm').fill('SAVE COMPUTER POLICY');
    await page.locator('#ccConfirmGo').click();
    await page.waitForFunction(() => document.getElementById('ccSaveStatus').dataset.tone === 'ok');
    const after = await api(page, '/api/computer/state');
    assert.equal(after.store.settings.levels.files, 'read');
    assert.notEqual(after.store.revision, before.store.revision);
    assert.match(await page.locator('#ccSaveStatus').innerText(), /Saved as/);
    assert.deepEqual(errors, []);
  } finally {
    await setLevels(page, 'off').catch(() => {});
    await context.close();
  }
});

test('files are scoped, previewed, written with a token and undone', async () => {
  const {page, context, errors} = await open();
  try {
    await setLevels(page, 'ask');
    await reload(page);
    await page.locator('#ccTabFiles').click();
    await page.waitForFunction(() => document.querySelectorAll('#ccTree .computer-tree-row').length > 0, {timeout: 15000});
    assert.match(await page.locator('#ccTreeNote').innerText(), /entries under/);
    assert.equal(await page.locator('#ccTree .computer-tree-row[data-kind="file"]').count() > 0, true);
    await openDir(page, 'tests/browser');
    await waitForTreeName(page, 'computer.test.mjs');

    await resetScratch();
    const created = await writeScratch(page, 'one\n');
    assert.ok(created.snapshot);
    await openDir(page, 'tests/browser');
    await waitForTreeName(page, '.cc-suite.txt');
    await page.locator('#ccTree .computer-tree-btn', {hasText: '.cc-suite.txt'}).first().click();
    await page.waitForFunction(() => document.getElementById('ccPreviewName').textContent.includes('.cc-suite.txt'));
    assert.equal(await page.locator('#ccText').inputValue(), 'one\n');

    await page.locator('#ccText').fill('two\n');
    await page.locator('#ccWrite').click();
    await page.waitForSelector('#ccConfirmRow:not([hidden])');
    assert.match(await page.locator('#ccConfirmLabel').innerText(), /WRITE tests\/browser\/\.cc-suite\.txt/);
    await page.locator('#ccConfirm').fill(`WRITE ${scratch}`);
    await page.locator('#ccConfirmGo').click();
    await page.waitForFunction(() => document.getElementById('ccFileStatus').dataset.tone === 'ok');
    const preview = await api(page, '/api/computer/file/preview', {path: scratch});
    assert.equal(preview.text, 'two\n');

    const newest = await api(page, '/api/computer/snapshots');
    const snapshot = newest.items.find((item) => item.path === scratch && item.status === 'applied');
    assert.ok(snapshot, 'the write is recorded as an applied snapshot');
    // Older runs leave applied snapshots for the same path; match this one by its id.
    const row = page.locator('#ccSnapshots .computer-list-row', {hasText: `#${snapshot.id.slice(0, 8)}`}).first();
    await row.locator('button').click();
    await page.waitForSelector('#ccConfirmRow:not([hidden])', {timeout: 10000});
    await page.locator('#ccConfirm').fill(`RESTORE ${snapshot.id}`);
    await page.locator('#ccConfirmGo').click();
    await page.waitForFunction(() => document.getElementById('ccFileStatus').textContent.includes('Restored'),
      null, {timeout: 15000});
    assert.equal((await api(page, '/api/computer/file/preview', {path: scratch})).text, 'one\n');
    assert.deepEqual(errors, []);
  } finally {
    await setLevels(page, 'off').catch(() => {});
    await resetScratch();
    await context.close();
  }
});

test('trash and restore move the real file and stay inside the workspace', async () => {
  const {page, context, errors} = await open();
  try {
    await setLevels(page, 'ask');
    await reload(page);
    await resetScratch();
    await writeScratch(page, 'trash me\n');
    await page.locator('#ccTabFiles').click();
    await openDir(page, 'tests/browser');
    await waitForTreeName(page, '.cc-suite.txt');
    await page.locator('#ccTree .computer-tree-btn', {hasText: '.cc-suite.txt'}).first().click();
    await page.waitForFunction(() => document.getElementById('ccPreviewName').textContent.includes('.cc-suite.txt'));
    await page.locator('#ccTrashButton').click();
    await page.waitForSelector('#ccConfirmRow:not([hidden])');
    await page.locator('#ccConfirm').fill(`TRASH ${scratch}`);
    await page.locator('#ccConfirmGo').click();
    await page.waitForFunction(() => document.getElementById('ccFileStatus').textContent.includes('trash'));
    const trash = await api(page, '/api/computer/trash');
    assert.equal(trash.entries.length >= 1, true);
    const entry = trash.entries.find((row) => row.path === scratch);
    assert.ok(entry, 'the trash records the original path');
    await fs.access(path.resolve('.cc-suite.txt')).then(() => { throw new Error('file should be gone'); },
      () => {});
    await page.locator('#ccSnapshots').scrollIntoViewIfNeeded();
    const restoreButton = page.locator('#ccTrash .computer-list-row button').first();
    await restoreButton.click();
    await page.waitForSelector('#ccConfirmRow:not([hidden])');
    await page.locator('#ccConfirm').fill(`RESTORE ${entry.id}`);
    await page.locator('#ccConfirmGo').click();
    await page.waitForFunction(() => document.getElementById('ccFileStatus').textContent.includes('Restored'));
    assert.equal(await fs.readFile(path.resolve('.cc-suite.txt'), 'utf8'), 'trash me\n');
    assert.deepEqual(errors, []);
  } finally {
    await resetScratch();
    await context.close();
  }
});

test('the browser tab reports the real reason it cannot inspect anything here', async () => {
  const {page, context, errors} = await open();
  try {
    await page.locator('#ccTabBrowser').click();
    const view = await api(page, '/api/computer/browser');
    assert.equal(view.available, false);
    assert.match(await page.locator('#ccBrowserReason').innerText(), /Unavailable|Playwright|allowlist/);
    assert.equal(await page.locator('#ccInspect').isDisabled(), true);
    assert.match(await page.locator('#ccOriginsList').innerText(), /No origin|^https?:/);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('panic needs the exact word, latches, and clears only when asked', async () => {
  const {page, context, errors} = await open();
  try {
    await page.locator('#ccTabPanic').click();
    assert.equal(await page.locator('#ccPanic').isDisabled(), true);
    await page.locator('#ccPanicWord').fill('PANIC');
    assert.equal(await page.locator('#ccPanic').isDisabled(), true);
    await page.locator('#ccPanicWord').fill('PANIC STOP');
    assert.equal(await page.locator('#ccPanic').isDisabled(), false);
    await page.locator('#ccPanic').click();
    await page.waitForFunction(() => document.getElementById('ccPanicStatus').dataset.tone === 'ok');
    await page.waitForFunction(() => !/clear|মুক্ত/i.test(document.getElementById('ccLatch').textContent), null,
      {timeout: 15000});
    assert.match(await page.locator('#ccLatch').innerText(), /Latched/);
    const latched = await api(page, '/api/computer/state');
    assert.equal(latched.latch.stopped, true);
    assert.equal(latched.queue.paused, true);

    assert.equal(await page.locator('#ccResume').isDisabled(), true);
    await page.locator('#ccResumeWord').fill('RESUME COMPUTER');
    await page.locator('#ccResumeQueue').check();
    assert.equal(await page.locator('#ccResume').isDisabled(), false);
    await page.locator('#ccResume').click();
    await page.waitForFunction(() => document.getElementById('ccResumeStatus').dataset.tone === 'ok');
    const cleared = await api(page, '/api/computer/state');
    assert.equal(cleared.latch.stopped, false);
    assert.equal(cleared.queue.paused, false);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('Bangla covers the page and the studio fits a 360 px phone', async () => {
  const {page, context, errors} = await open(360, 760);
  try {
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0);
    const english = await page.locator('#ccTabPermissions').innerText();
    await page.evaluate(() => window.nexusI18n.setLang('bn'));
    const bangla = await page.locator('#ccTabPermissions').innerText();
    assert.notEqual(english, bangla);
    assert.match(bangla, /[\u0980-\u09FF]/);
    assert.deepEqual(await page.evaluate(() => window.nexusI18n.missing()), []);
    await page.locator('#ccTabOverview').focus();
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.evaluate(() => window.nexusComputer.state().tab), 'permissions');
    assert.deepEqual(errors, []);
  } finally {
    await page.evaluate(() => window.nexusI18n.setLang('en')).catch(() => {});
    await context.close();
  }
});
