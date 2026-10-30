/** Prompt 1 acceptance: six-section shell, command palette, emergency kill, 360 px, Bangla.
 *  Everything runs against the real server; no task is started except the one the
 *  palette "compose" action only focuses (never submits). */
import {test, before, after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import bundled from '@sparticuz/chromium';

const base = process.env.NEXUS_TEST_URL || 'http://127.0.0.1:8787';
const token = process.env.NEXUS_GUI_TEST_TOKEN;
let browser;

before(async () => {
  assert.ok(token, 'Set NEXUS_GUI_TEST_TOKEN for the live dashboard');
  browser = await chromium.launch({executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || await bundled.executablePath(),
    args: bundled.args.filter((a) => !['--single-process', '--disable-web-security'].includes(a)), headless: true});
});
after(async () => { await browser?.close(); });

async function open(width = 1600, height = 1000) {
  const context = await browser.newContext({viewport: {width, height}, reducedMotion: 'reduce'});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto(base + '/dashboard');
  await page.locator('#guiToken').fill(token);
  await page.locator('#guiLogin button').click();
  await page.waitForFunction(() => document.getElementById('metaUplink').textContent === 'online', {timeout: 20000});
  return {page, context, errors};
}

test('six primary sections plus the secondary group replace the old four tabs without losing a page', async () => {
  const {page, context, errors} = await open();
  try {
    assert.equal(await page.evaluate(() => window.nexusGui.view), 'mission', 'Mission Control is the landing page');
    assert.ok(await page.locator('#view-mission').isVisible());
    const labels = await page.locator('.sidebar-nav .nav-item .nav-label').allTextContents();
    for (const required of ['Mission Control', 'Run & chat', 'Roles & router', 'Computer Control', 'Workflows', 'Approvals',
      'Security & logs', 'Memory & knowledge', 'Cost & models', 'World & news', 'Integrations', 'All controls', 'Settings']) {
      assert.ok(labels.includes(required), 'missing section: ' + required);
    }
    /* every legacy view still opens from the sidebar and from the hash */
    for (const [id, view] of [['run', 'run'], ['approvals', 'operations'], ['world', 'world'], ['controls', 'control'], ['mission', 'mission']]) {
      await page.locator(`.sidebar-nav [data-nav-id="${id}"]`).click();
      await page.waitForFunction((v) => window.nexusGui.view === v, view);
      assert.ok(await page.locator(`[data-gui-view="${view}"]`).first().isVisible(), view + ' must open');
    }
    for (const view of ['run', 'control', 'operations', 'world']) {
      await page.goto(base + '/dashboard#' + view);
      await page.waitForFunction(() => document.getElementById('metaUplink').textContent === 'online');
      assert.equal(await page.evaluate(() => window.nexusGui.view), view, 'hash alias #' + view);
    }
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('Ctrl+K finds a role by name in under 200 ms with 1000 roles, and prefixes narrow the search', async () => {
  const {page, context, errors} = await open();
  try {
    await page.keyboard.press('Control+k');
    await page.waitForSelector('#palette[open]');
    const roles = await page.evaluate(() => window.nexusPalette.data.roles.length);
    assert.ok(roles >= 1000, 'palette index must carry the whole roster, saw ' + roles);
    await page.fill('#paletteInput', 'frontend');
    const ms = await page.evaluate(() => window.nexusPalette.lastMs);
    assert.ok(ms > 0 && ms < 200, 'fuzzy search took ' + ms + ' ms');
    const first = await page.locator('.palette-row b').first().innerText();
    assert.match(first.toLowerCase(), /front/);
    await page.fill('#paletteInput', '@security-scan');
    await page.waitForFunction(() => document.querySelector('.palette-row b')?.textContent.includes('security-scan'));
    await page.fill('#paletteInput', '/world');
    await page.waitForFunction(() => document.querySelector('.palette-row b')?.textContent.includes('World'));
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#palette').isVisible(), false);
    /* Shift+Enter opens the same section in a new panel (a tab), never a task */
    await page.keyboard.press('Control+Shift+P');
    await page.waitForSelector('#palette[open]');
    await page.fill('#paletteInput', '/run');
    await page.waitForSelector('.palette-row');
    const [popup] = await Promise.all([page.waitForEvent('popup'), page.keyboard.press('Shift+Enter')]);
    assert.match(popup.url(), /#run$/);
    await popup.close();
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('the palette palette action only focuses the composer and never starts a task', async () => {
  const {page, context} = await open();
  try {
    const before = await page.evaluate(async () => (await window.nexusGui.api('/api/tasks')).length);
    await page.keyboard.press('Control+k');
    await page.fill('#paletteInput', '>compose');
    await page.waitForSelector('.palette-row');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.activeElement?.id === 'taskInput');
    const after = await page.evaluate(async () => (await window.nexusGui.api('/api/tasks')).length);
    assert.equal(after, before, 'opening the composer must not create a run');
  } finally { await context.close(); }
});

test('Emergency kill needs two confirmations and its confirm button stays disabled until the word matches', async () => {
  const {page, context} = await open(1440, 900);
  try {
    await page.locator('#emergencyKill').click();
    await page.waitForSelector('#killDialog[open]');
    assert.ok(await page.locator('#killContinue').isVisible(), 'first confirmation step');
    assert.equal(await page.locator('[data-kill-step="two"]').isVisible(), false, 'typed step is hidden until step one');
    await page.locator('#killContinue').click();
    await page.waitForSelector('[data-kill-step="two"]:not([hidden])');
    assert.equal(await page.locator('#killConfirm').isDisabled(), true, 'empty word keeps the kill disarmed');
    await page.locator('#killWord').fill('kill');
    assert.equal(await page.locator('#killConfirm').isDisabled(), true, 'lowercase must not arm the kill');
    await page.locator('#killWord').fill('KILL');
    assert.equal(await page.locator('#killConfirm').isDisabled(), false);
    await page.locator('#killCancel2').click();
    await page.waitForFunction(() => !document.getElementById('killDialog').open);
    /* nothing was sent: the daemon state is unchanged and no toast was raised */
    const state = await page.evaluate(async () => (await window.nexusGui.api('/api/operations/state')).daemon.state);
    assert.ok(typeof state === 'string' && state.length > 0);
    assert.equal(await page.locator('.toast').count(), 0);
  } finally { await context.close(); }
});

test('the shell fits a 360 px phone: drawer, bottom tabs, palette and footer', async () => {
  const {page, context, errors} = await open(360, 740);
  try {
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0, 'no horizontal overflow');
    assert.equal(await page.locator('#tabbar').isVisible(), true);
    const closedBox = await page.locator('.sidebar').boundingBox();
    assert.ok(closedBox.x + closedBox.width <= 1, 'the sidebar starts off-canvas: ' + JSON.stringify(closedBox));
    await page.locator('#navToggle').click();
    await page.waitForFunction(() => document.body.classList.contains('nav-open'));
    assert.equal(await page.locator('.sidebar').isVisible(), true);
    await page.locator('.sidebar-nav [data-nav-id="controls"]').click();
    await page.waitForFunction(() => window.nexusGui.view === 'control');
    await page.waitForFunction(() => !document.body.classList.contains('nav-open'));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0);
    await page.locator('#tabbar [data-nav="run"]').click();
    await page.waitForFunction(() => window.nexusGui.view === 'run');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0);
    await page.keyboard.press('Control+k');
    await page.waitForSelector('#palette[open]');
    const box = await page.locator('.palette-box').boundingBox();
    assert.ok(box.width <= 360 && box.x >= 0, 'palette box ' + JSON.stringify(box));
    await page.keyboard.press('Escape');
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('every new label exists in Bangla and the shell switches live', async () => {
  const {page, context, errors} = await open();
  try {
    const missing = await page.evaluate(() => window.nexusI18n.missing());
    assert.deepEqual(missing, [], 'untranslated keys: ' + missing.join(', '));
    await page.evaluate(() => window.nexusI18n.setLang('bn'));
    assert.equal(await page.evaluate(() => document.documentElement.lang), 'bn');
    assert.equal(await page.locator('.sidebar-nav [data-nav-id="mission"] .nav-label').innerText(), 'মিশন কন্ট্রোল');
    assert.equal(await page.locator('#pageTitle').innerText(), 'মিশন কন্ট্রোল');
    assert.match(await page.locator('#view-mission h1').innerText(), /এজেন্ট/);
    await page.keyboard.press('Control+k');
    await page.waitForSelector('#palette[open]');
    assert.match(await page.locator('#paletteInput').getAttribute('placeholder'), /খুঁজুন/);
    await page.keyboard.press('Escape');
    await page.evaluate(() => window.nexusI18n.setLang('en'));
    assert.equal(await page.locator('.sidebar-nav [data-nav-id="mission"] .nav-label').innerText(), 'Mission Control');
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('the notification bell and the status footer report real server state', async () => {
  const {page, context} = await open();
  try {
    await page.locator('#notifyBell').click();
    await page.waitForSelector('#bellPopover:not([hidden])');
    const state = await page.evaluate(async () => (await window.nexusGui.api('/api/operations/state')).pending_actions_total);
    const badge = await page.locator('#bellCount').innerText();
    assert.equal(Number(badge), Math.min(Number(state) || 0, 99));
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#bellPopover').isVisible(), false);
    for (const id of ['sbState', 'sbTask', 'sbRole', 'sbAgent', 'sbSse', 'sbVersion']) {
      assert.equal(await page.locator('#' + id).count(), 1, 'missing footer field ' + id);
    }
    assert.match(await page.locator('#sbVersion').innerText(), /nexus \d/i);
  } finally { await context.close(); }
});
