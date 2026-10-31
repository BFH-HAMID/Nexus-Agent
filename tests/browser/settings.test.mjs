/** Prompt 13 acceptance: one Settings page for every stored preference.

 *  The page must show where each choice lives (browser vs workspace), read one
 *  honest snapshot, keep the sidebar quick toggles in step, translate live, and
 *  open only panes that already exist. Opening a control never executes a task
 *  and never grants a permission — the run count is asserted before and after.
 */
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import {fileURLToPath} from 'node:url';
import bundled from '@sparticuz/chromium';

const base = process.env.NEXUS_TEST_URL || 'http://127.0.0.1:8787';
const token = process.env.NEXUS_GUI_TEST_TOKEN;
const shot = (name) => fileURLToPath(new URL('../../docs/images/gui-' + name + '.png', import.meta.url));

async function open(browser, width = 1500, height = 1100) {
  const context = await browser.newContext({viewport: {width, height}, reducedMotion: 'reduce'});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(base + '/dashboard');
  await page.locator('#guiToken').fill(token);
  await page.locator('#guiLogin button').click();
  await page.waitForFunction(() => document.getElementById('metaUplink').textContent === 'online', null, {timeout: 20000});
  return {page, context, errors};
}

async function settings(page) {
  await page.locator('.sidebar-nav [data-nav-id="settings"]').click();
  await page.waitForFunction(() => window.nexusGui.view === 'settings', null, {timeout: 10000});
  await page.waitForFunction(() => window.nexusSettings?.readAt > 0, null, {timeout: 10000});
}

test('the hub reports the running build and where every preference lives', async () => {
  assert.ok(token, 'Set NEXUS_GUI_TEST_TOKEN for the live dashboard');
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || await bundled.executablePath(),
    args: bundled.args.filter((a) => !['--single-process', '--disable-web-security'].includes(a)), headless: true});
  const {page, context, errors} = await open(browser);
  try {
    const health = await page.evaluate(async () => (await window.nexusGui.api('/api/health')));
    await settings(page);
    assert.equal(await page.locator('#pageTitle').innerText(), 'Settings');
    assert.equal(await page.locator('#view-settings h1').innerText(), 'Settings');

    /* the snapshot is the server's, at a printed instant */
    assert.match(await page.locator('#settingsStamp').innerText(), /Observed .*local time/);
    const facts = await page.evaluate(() => window.nexusSettings.data);
    assert.equal(facts.build.version, health.version);
    assert.equal(facts.build.roles, health.roles);
    assert.equal(facts.build.agents, health.agents.length);
    assert.deepEqual(facts.display.keys, ['nexus.ui.theme', 'nexus.ui.compact', 'nexus.ui.still', 'nexus.ui.lang']);
    assert.ok(facts.notes.includes('presentation_only'));

    /* the build card shows the same numbers, not placeholders */
    const build = await page.locator('#settingsBuildFacts').innerText();
    assert.match(build, new RegExp(String(health.roles)));
    for (const label of ['PANE LANGUAGE', 'WORLD ENABLED', 'WEATHER PROVIDER', 'TOKEN REQUIRED']) {
      assert.ok((await page.locator('#view-settings').innerText()).includes(label), 'missing fact ' + label);
    }
    assert.equal(await page.locator('iframe').count(), 0);
    await page.screenshot({path: shot('settings'), fullPage: true, animations: 'disabled'});
    assert.deepEqual(errors, []);
  } finally { await context.close(); await browser.close(); }
});

test('appearance and language are edited in one place and all surfaces agree', async () => {
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || await bundled.executablePath(),
    args: bundled.args.filter((a) => !['--single-process', '--disable-web-security'].includes(a)), headless: true});
  const {page, context, errors} = await open(browser);
  try {
    await settings(page);
    const tasksBefore = await page.evaluate(async () => (await window.nexusGui.api('/api/tasks')).length);

    /* theme */
    await page.locator('[data-theme-choice="light"]').click();
    assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), 'light');
    assert.equal(await page.locator('[data-theme-choice="light"]').getAttribute('aria-pressed'), 'true');
    await page.locator('[data-theme-choice="dark"]').click();
    assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), 'dark');

    /* the two display switches must be the same state the sidebar row shows */
    await page.locator('#settingsCompact').click();
    assert.equal(await page.evaluate(() => document.body.classList.contains('ui-compact')), true);
    assert.equal(await page.locator('.ui-preferences button[data-pref="compact"]').getAttribute('aria-pressed'), 'true');
    await page.locator('.ui-preferences button[data-pref="still"]').click();
    assert.equal(await page.locator('#settingsStill').getAttribute('aria-pressed'), 'true');
    assert.equal(await page.evaluate(() => document.body.classList.contains('ui-still')), true);

    /* reset returns to the documented defaults */
    await page.locator('#settingsResetDisplay').click();
    assert.equal(await page.evaluate(() => document.body.classList.contains('ui-compact') || document.body.classList.contains('ui-still')), false);
    assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), 'dark');
    assert.match(await page.locator('#settingsStatus').innerText(), /reset/i);

    /* language: dashboard, sidebar and the workspace copy move together */
    await page.locator('[data-lang-choice="bn"]').click();
    await page.waitForFunction(() => document.documentElement.lang === 'bn', null, {timeout: 5000});
    assert.equal(await page.locator('#pageTitle').innerText(), 'সেটিংস');
    assert.equal(await page.locator('#view-settings h1').innerText(), 'সেটিংস');
    assert.equal(await page.locator('.sidebar-nav [data-nav-id="settings"] .nav-label').innerText(), 'সেটিংস');
    assert.match(await page.locator('#settingsLanguageChip').innerText(), /ওয়ার্কস্পেস/);
    await page.screenshot({path: shot('settings-bn'), fullPage: true, animations: 'disabled'});
    const mirrored = await page.evaluate(async () => (await window.nexusGui.api('/api/workbench/settings')).data.language);
    assert.equal(mirrored, 'bn', 'the Workbench panes must keep the same language');

    /* it survives a reload because the browser remembers it */
    await page.reload();
    await page.waitForFunction(() => document.getElementById('metaUplink').textContent === 'online', null, {timeout: 20000});
    assert.equal(await page.evaluate(() => document.documentElement.lang), 'bn');
    await settings(page);
    await page.locator('[data-lang-choice="en"]').click();
    await page.waitForFunction(() => document.documentElement.lang === 'en', null, {timeout: 5000});
    assert.equal(await page.evaluate(async () => (await window.nexusGui.api('/api/workbench/settings')).data.language), 'en');

    const tasksAfter = await page.evaluate(async () => (await window.nexusGui.api('/api/tasks')).length);
    assert.equal(tasksAfter, tasksBefore, 'changing preferences must not start a task');
    assert.deepEqual(errors, []);
  } finally {
    /* leave the shared workspace in English even if an assertion failed midway */
    try { await page.evaluate(() => window.nexusGui.api('/api/workbench/settings', {language: 'en'})); } catch { /* page may be gone */ }
    await context.close();
    await browser.close();
  }
});

test('every button opens an existing pane and nothing else', async () => {
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || await bundled.executablePath(),
    args: bundled.args.filter((a) => !['--single-process', '--disable-web-security'].includes(a)), headless: true});
  const {page, context, errors} = await open(browser);
  try {
    await settings(page);
    const tasksBefore = await page.evaluate(async () => (await window.nexusGui.api('/api/tasks')).length);

    await page.locator('#settingsOpenLayout').click();
    await page.waitForFunction(() => document.getElementById('workbench').open && !document.getElementById('wbPane-layout').hidden, null, {timeout: 8000});
    assert.equal(await page.locator('#wbLanguage').count(), 1, 'the real layout pane, not a copy');
    await page.locator('#wbClose').click();

    await page.locator('#settingsOpenDesktopSetup').click();
    await page.waitForFunction(() => document.getElementById('workbench').open && !document.getElementById('wbPane-desktopSetup').hidden, null, {timeout: 8000});
    assert.equal(await page.locator('#dsTabs').isVisible(), true);
    await page.locator('#wbClose').click();

    await page.locator('#settingsOpenWorldSettings').click();
    await page.waitForFunction(() => document.getElementById('workbench').open && !document.getElementById('wbPane-worldSettings').hidden, null, {timeout: 8000});
    await page.waitForFunction(() => document.getElementById('wsSources').children.length > 0, null, {timeout: 8000});
    await page.locator('#wbClose').click();

    await page.locator('#settingsOpenWorld').click();
    await page.waitForFunction(() => window.nexusGui.view === 'world', null, {timeout: 8000});

    await settings(page);
    await page.locator('[data-goto="security"]').first().click();
    await page.waitForFunction(() => window.nexusGui.view === 'security', null, {timeout: 8000});

    await settings(page);
    await page.locator('#settingsOpenWorkbench').click();
    await page.waitForFunction(() => document.getElementById('workbench').open, null, {timeout: 8000});
    await page.locator('#wbClose').click();

    const tasksAfter = await page.evaluate(async () => (await window.nexusGui.api('/api/tasks')).length);
    assert.equal(tasksAfter, tasksBefore, 'opening panes must not create a run');
    assert.deepEqual(errors, []);
  } finally { await context.close(); await browser.close(); }
});

test('the hub is readable at 360 px, keeps its order, and never overflows', async () => {
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || await bundled.executablePath(),
    args: bundled.args.filter((a) => !['--single-process', '--disable-web-security'].includes(a)), headless: true});
  const {page, context, errors} = await open(browser, 360, 740);
  try {
    await page.locator('#navToggle').click();
    await page.waitForFunction(() => document.body.classList.contains('nav-open'));
    await page.locator('.sidebar-nav [data-nav-id="settings"]').click();
    await page.waitForFunction(() => window.nexusGui.view === 'settings', null, {timeout: 10000});
    await page.waitForFunction(() => window.nexusSettings?.readAt > 0, null, {timeout: 10000});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0, 'no horizontal overflow');
    const columns = await page.locator('.settings-grid').evaluate((node) => getComputedStyle(node).gridTemplateColumns.split(' ').length);
    assert.equal(columns, 1, 'one column on a phone');
    /* every control is still a real, reachable button */
    for (const id of ['settingsCompact', 'settingsStill', 'settingsResetDisplay', 'settingsReload']) {
      assert.equal(await page.locator('#' + id).isVisible(), true, id + ' must stay reachable');
    }
    await page.locator('#settingsCompact').click();
    assert.equal(await page.evaluate(() => document.body.classList.contains('ui-compact')), true);
    await page.locator('#settingsCompact').click();
    await page.screenshot({path: shot('settings-mobile-360'), fullPage: true, animations: 'disabled'});
    assert.deepEqual(errors, []);
  } finally { await context.close(); await browser.close(); }
});
