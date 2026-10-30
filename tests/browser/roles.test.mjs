/** Prompt 3 acceptance: the Role Hub reads the roster in pages, never ships a
 *  prompt in the index, previews a route without calling a model, and every
 *  edit goes through the reviewed configuration path with typed consent. */
import {test, before, after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import bundled from '@sparticuz/chromium';

const base = process.env.NEXUS_TEST_URL || 'http://127.0.0.1:8787';
const token = process.env.NEXUS_GUI_TEST_TOKEN;
let browser;

before(async () => {
  assert.ok(token, 'Set NEXUS_GUI_TEST_TOKEN for the live dashboard');
  browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || await bundled.executablePath(),
    args: bundled.args.filter((a) => !['--single-process', '--disable-web-security'].includes(a)),
    headless: true,
  });
});
after(async () => { await browser?.close(); });

async function open(width = 1600, height = 1000) {
  const context = await browser.newContext({viewport: {width, height}, reducedMotion: 'reduce', acceptDownloads: true});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(base + '/dashboard');
  await page.locator('#guiToken').fill(token);
  await page.locator('#guiLogin button').click();
  await page.waitForFunction(() => document.getElementById('metaUplink').textContent === 'online', {timeout: 20000});
  if (width > 900) await page.locator('.sidebar-nav [data-nav-id="roles"]').click();
  else await page.evaluate(() => window.nexusGui.show('roles'));
  await page.waitForSelector('#view-roles .roles-row', {timeout: 20000});
  return {page, context, errors};
}

const rowCount = (page) => page.locator('#view-roles .roles-row').count();

test('the hub pages the roster, keeps prompts out of the list and stamps its read', async () => {
  const {page, context, errors} = await open();
  try {
    const data = await page.evaluate(async () => (await window.nexusGui.api('/api/roles/index')).data);
    assert.equal(data.schema, 1);
    assert.ok(data.total >= 100, 'roster size ' + data.total);
    assert.equal(data.page_size, 50);
    assert.equal(data.roles.length, Math.min(50, data.total));
    assert.ok(!('persona' in data.roles[0]), 'the index must not ship prompt text');
    assert.ok(data.stats.categories > 1);
    assert.match(await page.locator('#rhStats').innerText(), /roles · .*workspace edits/);
    assert.match(await page.locator('#rhUpdated').innerText(), /^Read from the server at \d/);
    assert.match(await page.locator('#rhMeta').innerText(), /^1–(\d+) of \d+ matched/);
    /* virtualised: only a window of rows exists in the DOM, but the spacer is
       as tall as the whole page of results */
    const rows = await rowCount(page);
    assert.ok(rows > 5 && rows < 50, 'rendered window ' + rows);
    const spacer = await page.locator('#view-roles .roles-spacer').evaluate((node) => node.style.height);
    assert.equal(spacer, `${data.roles.length * 46}px`);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('search, filter and sort are answered by the server', async () => {
  const {page, context, errors} = await open();
  try {
    await page.locator('#rhSearch').fill('security');
    await page.waitForFunction(() => {
      const state = window.nexusRoleHub.state();
      return state.q === 'security' && state.matched < state.total;
    }, null, {timeout: 20000});
    const meta = await page.locator('#rhMeta').innerText();
    const matched = Number(meta.match(/of (\d+) matched/)[1]);
    assert.ok(matched > 0 && matched < 1000, meta);

    await page.locator('#rhSort').selectOption('persona');
    await page.waitForFunction(() => window.nexusRoleHub.state().sort === 'persona');
    /* the sort is applied by the server, not by re-ordering a cached page */
    const sizes = await page.evaluate(async () => (await window.nexusGui.api(
      '/api/roles/index?sort=persona&dir=desc&size=10')).data.roles.map((row) => row.persona_chars));
    assert.deepEqual(sizes, [...sizes].sort((a, b) => b - a));
    assert.ok((await page.locator('#view-roles .roles-row b').allInnerTexts()).length > 0);

    await page.locator('#rhCategory').selectOption({index: 1});
    await page.waitForFunction(() => Boolean(window.nexusRoleHub.state().category));
    const category = await page.evaluate(() => window.nexusRoleHub.state().category);
    const categories = await page.evaluate(() => window.nexusRoleHub.state().filters.categories);
    assert.ok(categories.includes(category));

    await page.locator('#rhSearch').fill('');
    await page.locator('#rhCategory').selectOption('');
    await page.waitForFunction(() => window.nexusRoleHub.state().q === '' && window.nexusRoleHub.state().category === '');
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('the list is keyboard operable and opens the highlighted role', async () => {
  const {page, context, errors} = await open();
  try {
    await page.locator('#rhList').focus();
    const start = await page.locator('#rhTitle').innerText();
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('Enter');
    await page.waitForFunction((first) => document.getElementById('rhName').textContent.length > 0
      && document.getElementById('rhName').textContent !== '—' && document.getElementById('rhTitle').textContent !== first,
      start, {timeout: 15000});
    const selected = await page.locator('#rhName').innerText();
    assert.equal(await page.getAttribute('#rhList', 'aria-activedescendant'), 'rhRow-' + selected);
    assert.equal(await page.locator('#view-roles .roles-row[aria-selected="true"]').count(), 1);
    /* the detail really is that role */
    const detail = await page.evaluate(async (name) => (await window.nexusGui.api('/api/roles/detail?name=' + encodeURIComponent(name))).data, selected);
    assert.equal(await page.locator('#rhFPersona').inputValue(), detail.persona);
    assert.match(await page.locator('#rhHint').innerText(), /Read from/);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('the route preview names a role and reports zero model calls', async () => {
  const {page, context, errors} = await open();
  try {
    await page.locator('#rhTabRoute').click();
    await page.locator('#rhRouteTask').fill('build a python api with docker and postgres');
    await page.locator('#rhRouteGo').click();
    await page.waitForSelector('#rhRouteOut .roles-routehead', {timeout: 15000});
    const head = await page.locator('#rhRouteOut .roles-routehead').innerText();
    assert.match(head, /Keyword routing picks|Nothing reaches the threshold/);
    assert.match(await page.locator('#rhRouteStatus').innerText(), /0 model calls/);
    assert.match(head, /keyword/, 'the method is stated');
    const candidates = await page.locator('.roles-candidate').count();
    assert.ok(candidates >= 1);
    /* nothing was submitted as a task */
    const tasks = await page.evaluate(async () => (await window.nexusGui.api('/api/tasks')));
    assert.equal(Array.isArray(tasks) ? tasks.length : 0, 0, 'a preview must not create a task');
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('quality lists real findings and says what it does not check', async () => {
  const {page, context, errors} = await open();
  try {
    await page.locator('#rhTabQuality').click();
    await page.waitForSelector('#rhIssues li[data-severity]', {timeout: 20000});
    const meta = await page.locator('#rhQualityMeta').innerText();
    assert.match(meta, /\d+ roles · \d+ high · \d+ low · \d+ notes/);
    const severities = await page.locator('#rhIssues li').evaluateAll((nodes) => nodes.map((n) => n.dataset.severity));
    assert.ok(severities.includes('high'), severities.join(','));
    assert.match(await page.locator('#rhQualityScope').innerText(), /Not checked:/);
    /* a finding opens the offending role */
    await page.locator('#rhIssues li[data-severity="high"] .roles-issue').first().click();
    await page.waitForFunction(() => document.getElementById('rhName').textContent.length > 0
      && !document.getElementById('rhForm').hidden);
    assert.ok((await page.locator('#rhFPersona').inputValue()).length > 0);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('an edit needs typed consent, is versioned, diffable and restorable', async () => {
  const {page, context, errors} = await open();
  const name = 'zz-hub-acceptance';
  try {
    page.once('dialog', (dialog) => {
      assert.match(dialog.message(), /Role name/);
      dialog.accept(name);
    });
    await page.locator('#rhNew').click();
    await page.waitForFunction((roleName) => !document.getElementById('rhForm').hidden
      && document.getElementById('rhName').textContent === roleName, name);
    await page.locator('#rhFPersona').fill('You are the Role Hub acceptance fixture.');
    await page.locator('#rhFTitle').fill('Acceptance fixture');
    await page.locator('#rhFKeywords').fill('zzhubaccept:2');
    await page.locator('#rhFCategory').fill('testing');
    await page.locator('#rhFAgent').selectOption({index: 0});
    await page.evaluate(() => {
      const node = document.getElementById('rhStatus');
      node.textContent = '';
      node.removeAttribute('data-tone');
    });

    /* a wrong confirmation must change nothing */
    page.once('dialog', (dialog) => dialog.accept('SAVE ROLE something-else'));
    await page.locator('#rhSave').click();
    await page.waitForFunction(() => document.getElementById('rhStatus').dataset.tone === 'error', null, {timeout: 20000});
    assert.match(await page.locator('#rhStatus').innerText(), /Cancelled|confirmation/i);

    /* the real one */
    page.once('dialog', (dialog) => {
      assert.match(dialog.message(), new RegExp('SAVE ROLE ' + name));
      dialog.accept('SAVE ROLE ' + name);
    });
    await page.locator('#rhSave').click();
    await page.waitForFunction(() => {
      const node = document.getElementById('rhStatus');
      return node.dataset.tone === 'ok' && document.getElementById('view-roles').dataset.busy === 'false';
    }, null, {timeout: 30000});
    const saved = await page.evaluate(async (roleName) => (await window.nexusGui.api('/api/roles/detail?name=' + roleName)).data, name);
    assert.equal(saved.title, 'Acceptance fixture');
    assert.equal(saved.override, true);

    /* second edit records the first version */
    await page.locator('#rhFTitle').fill('Acceptance fixture v2');
    await page.evaluate(() => {
      const node = document.getElementById('rhStatus');
      node.textContent = '';
      node.removeAttribute('data-tone');
    });
    page.once('dialog', (dialog) => dialog.accept('SAVE ROLE ' + name));
    await page.locator('#rhSave').click();
    await page.waitForFunction(() => document.getElementById('rhStatus').dataset.tone === 'ok'
      && document.getElementById('view-roles').dataset.busy === 'false', null, {timeout: 30000});
    assert.equal(await page.locator('#rhFTitle').inputValue(), 'Acceptance fixture v2');

    await page.locator('#rhTabHistory').click();
    await page.waitForSelector('#rhVersions .roles-version', {timeout: 20000});
    /* The newest snapshot is the file as it was before this last edit. */
    const history = await page.evaluate(async () => (await window.nexusGui.api('/api/roles/history')).data.versions);
    const version = history[0].id;
    assert.match(version, /^\d{8}T\d{6}-[0-9a-f]{6}/);
    assert.equal(history[0].reason, 'save ' + name);
    await page.locator(`#rhVersions [data-id="${version}"]`).click();
    await page.waitForFunction(() => !document.getElementById('rhDiff').hidden, null, {timeout: 15000});
    const diff = await page.locator('#rhDiff').innerText();
    assert.match(diff, new RegExp(name));
    assert.match(diff, /\+\s+title: Acceptance fixture v2/);
    assert.match(diff, /-\s+title: Acceptance fixture(\s|$)/);

    /* restore the recorded version, then remove the fixture again */
    await page.evaluate(() => {
      const node = document.getElementById('rhRestoreStatus');
      node.textContent = '';
      node.removeAttribute('data-tone');
    });
    page.once('dialog', (dialog) => dialog.accept('RESTORE ROLES ' + version));
    await page.locator('#rhRestore').click();
    await page.waitForFunction(() => document.getElementById('rhRestoreStatus').dataset.tone === 'ok'
      && document.getElementById('view-roles').dataset.busy === 'false', null, {timeout: 30000});
    await page.waitForFunction(() => document.getElementById('rhFTitle').value === 'Acceptance fixture', null, {timeout: 20000});

    await page.locator('#rhTabRole').click();
    await page.evaluate(() => {
      const node = document.getElementById('rhStatus');
      node.textContent = '';
      node.removeAttribute('data-tone');
    });
    page.once('dialog', (dialog) => dialog.accept('REMOVE ROLE ' + name));
    await page.locator('#rhRemove').click();
    await page.waitForFunction(() => document.getElementById('rhStatus').dataset.tone === 'ok', null, {timeout: 30000});
    const gone = await page.evaluate(async (roleName) => {
      const response = await window.nexusGui.fetch('/api/roles/detail?name=' + roleName);
      return response.status;
    }, name);
    assert.equal(gone, 400, 'the fixture is gone after REMOVE');
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('export downloads YAML for the open role', async () => {
  const {page, context, errors} = await open();
  try {
    await page.locator('#rhList').focus();
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.getElementById('rhName').textContent.length > 0);
    const [download] = await Promise.all([
      page.waitForEvent('download', {timeout: 20000}),
      page.locator('#rhExportOne').click(),
    ]);
    assert.match(download.suggestedFilename(), /^roles-export-\d{8}-\d{6}\.yaml$/);
    assert.match(await page.locator('#rhStatus').innerText(), /Exported 1 role/);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('the hub is translated, has no overflow at 360 px and keeps its footer', async () => {
  const {page, context, errors} = await open(360, 740);
  try {
    await page.evaluate(() => window.nexusI18n.setLang('bn'));
    await page.waitForFunction(() => document.documentElement.lang === 'bn');
    assert.deepEqual(await page.evaluate(() => window.nexusI18n.missing()), []);
    assert.equal(await page.locator('#rhStats').evaluate((node) => node.textContent.includes('রোল')), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0);
    assert.equal(await page.locator('#view-roles .roles-tabs button').first().innerText(), 'রোল');
    await page.evaluate(() => window.nexusI18n.setLang('en'));
    assert.equal(await page.locator('#rhTabQuality').innerText(), 'Quality');
    assert.match(await page.locator('#rhUpdated').innerText(), /^Read from the server at/);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});
