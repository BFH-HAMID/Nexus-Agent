/** Prompt 7 acceptance: the Workflow Builder, its versions, runs and schedules.
 *
 *  Everything here is created through the real API: versions go through the
 *  Studio's immutable store, the run goes through the actual DAG engine (a lint
 *  verification node, no model call), and schedules are real records in
 *  `.nexus/workflows`. The suite removes only the schedules it created; saved
 *  versions are immutable by design and stay in the runtime store.
 */
import {test, before, after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import bundled from '@sparticuz/chromium';

const base = process.env.NEXUS_TEST_URL || 'http://127.0.0.1:8787';
const token = process.env.NEXUS_GUI_TEST_TOKEN;
const RUN = Date.now().toString(36);
const PREFIX = `Workflow Builder ${RUN} `;
const NAMESPACE = 'Workflow Builder ';
let browser;
let page;
let context;
let errors = [];
let savedId = '';

const headers = () => ({'X-Nexus-Token': token, 'Content-Type': 'application/json'});

async function api(route, body) {
  const reply = await fetch(base + route, {
    method: body === undefined ? 'GET' : 'POST', headers: headers(),
    ...(body === undefined ? {} : {body: JSON.stringify(body)}),
  });
  const payload = await reply.json();
  if (!reply.ok) throw new Error(payload?.error?.message || reply.status);
  return payload.data;
}

/** Only this suite's own schedules are ever removed, matched by their prefix. */
async function sweepOwn() {
  const body = await api('/api/workflows/schedules').catch(() => ({schedules: []}));
  for (const row of body.schedules || []) {
    if (!String(row.name || '').startsWith(NAMESPACE)) continue;
    await api('/api/workflows/schedule/remove', {id: row.id, confirmation: `REMOVE SCHEDULE ${row.id}`})
      .catch(() => {});
  }
}

before(async () => {
  assert.ok(token, 'Set NEXUS_GUI_TEST_TOKEN for the live dashboard');
  await sweepOwn();
  browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || await bundled.executablePath(),
    args: bundled.args.filter((a) => !['--single-process', '--disable-web-security'].includes(a)),
    headless: true,
  });
  context = await browser.newContext({viewport: {width: 1600, height: 1000}, reducedMotion: 'reduce'});
  page = await context.newPage();
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(base + '/dashboard');
  await page.locator('#guiToken').fill(token);
  await page.locator('#guiLogin button').click();
  await page.waitForFunction(() => document.getElementById('metaUplink').textContent === 'online', {timeout: 20000});
  await page.locator('.sidebar-nav [data-nav-id="workflows"]').click();
  await page.waitForFunction(() => document.querySelectorAll('#wfKpis .workflows-count').length >= 4, {timeout: 20000});
});

after(async () => {
  await sweepOwn();
  await context?.close();
  await browser?.close();
});

/** Type a token into one confirmation box and press its button. */
async function confirm(scope, word) {
  const input = page.locator(`${scope} .workflows-word`).last();
  await input.waitFor({timeout: 10000});
  await input.fill(word);
  const button = page.locator(`${scope} .workflows-confirmrow button`).first();
  assert.equal(await button.isEnabled(), true);
  await button.click();
}

test('the sidebar opens the builder and every count comes from the API', async () => {
  assert.equal(await page.locator('#view-workflows').isVisible(), true);
  const families = await page.evaluate(async () => (await window.nexusGui.api('/api/workflows/families')).data.count);
  const text = await page.locator('#wfKpis .workflows-count').first().textContent();
  assert.equal(text.startsWith(String(families)), true, 'family count must match /api/workflows/families');
  /* the typed tokens the page asks for are the ones the server publishes */
  const limits = await page.locator('#wfLimits').textContent();
  assert.match(limits, /SAVE WORKFLOW/);
  assert.match(limits, /SCHEDULE WORKFLOW/);
  assert.match(limits, /RUN REVIEWED WORKFLOW/);
  assert.deepEqual(errors, []);
});

test('a draft is validated without writing anything', async () => {
  const before = await page.evaluate(async () => (await window.nexusGui.api('/api/workflows/families')).data.count);
  await page.locator('#wfTabWorkflows').click();
  await page.locator('#wfNew').click();
  await page.waitForFunction(() => document.querySelectorAll('#wfNodeList .workflows-nodeRow').length === 2, {timeout: 15000});
  await page.locator('#wfDraftName').fill(`${PREFIX}draft`);
  await page.locator('#wfDraftName').blur();
  /* replace the template with one verification node so a real run is cheap */
  await page.locator('#wfNodeList .workflows-remove').first().click();
  await page.locator('#wfNodeList .workflows-remove').first().click();
  await page.waitForFunction(() => document.querySelectorAll('#wfNodeList .workflows-nodeRow').length === 0, {timeout: 15000});
  await page.locator('#wfAddRow [data-wf-add="verification"]').click();
  await page.locator('#wfInspector select').nth(1).selectOption('lint');
  await page.waitForTimeout(200);
  await page.locator('#wfValidate').click();
  await page.waitForFunction(() => document.getElementById('wfValidateOut').hidden === false, {timeout: 15000});
  const result = await page.locator('#wfValidateOut').textContent();
  assert.match(result, /Order:/);
  assert.match(result, /No model call, command or write happened/);
  const after = await page.evaluate(async () => (await window.nexusGui.api('/api/workflows/families')).data.count);
  assert.equal(after, before, 'validating must not create a version');
});

test('saving needs the exact token and the poll never eats what you typed', async () => {
  await page.locator('#wfSave').click();
  const input = page.locator('#wfConfirmHost .workflows-word');
  await input.waitFor({timeout: 10000});
  await input.fill('SAVE WORKFL');
  await page.waitForTimeout(9000);                      // two 8 s poll cycles pass
  assert.equal(await input.inputValue(), 'SAVE WORKFL', 'a background poll must not rebuild an open form');
  const go = page.locator('#wfConfirmHost .workflows-confirmrow button').first();
  assert.equal(await go.isDisabled(), true, 'a partial token must not save');
  await input.fill('SAVE WORKFLOW');
  await go.click();
  await page.waitForFunction(() => document.getElementById('wfDraftSource').textContent.startsWith('From'), {timeout: 20000});
  savedId = (await page.locator('#wfDraftSource').textContent()).match(/From ([\w-]+)/)[1];
  assert.match(await page.locator('#wfDraftSource').textContent(), /v1/);
  const saved = await api(`/api/workflows/version?id=${savedId}`);
  assert.equal(saved.name, `${PREFIX}draft`);
  assert.equal(saved.definition.nodes.length, 1);
  assert.equal(saved.definition.nodes[0].kind, 'verification');
  assert.equal(saved.definition.nodes[0].check, 'lint');
});

test('a second save keeps the first version and the diff names the change', async () => {
  await page.locator('#wfInspector textarea').fill('Run the lint check after the review.');
  await page.waitForTimeout(100);
  await page.locator('#wfInspector textarea').blur();
  await page.locator('#wfSave').click();
  await confirm('#wfConfirmHost', 'SAVE WORKFLOW');
  await page.waitForFunction(() => /v2/.test(document.getElementById('wfDraftSource').textContent), {timeout: 20000});
  await page.locator('#wfTabWorkflows').click();
  const versions = page.locator('#wfFamilies .workflows-item', {hasText: `${PREFIX}draft`}).locator('.workflows-versions li');
  await page.waitForFunction((prefix) => [...document.querySelectorAll('#wfFamilies .workflows-item')]
    .some((row) => row.textContent.includes(prefix) && row.querySelectorAll('.workflows-versions li').length >= 2),
  `${PREFIX}draft`, {timeout: 20000});
  assert.equal(await versions.count(), 2);
  await page.locator('#wfTabBuilder').click();
  await page.locator('#wfDiffA').selectOption({index: 1});
  const options = await page.evaluate(() => [...document.getElementById('wfDiffB').options].map((o) => o.value));
  assert.equal(options.includes(savedId), true);
  await page.locator('#wfDiffB').selectOption({index: 2});
  await page.locator('#wfDiffGo').click();
  await page.waitForFunction(() => document.querySelectorAll('#wfDiffOut .workflows-plain li').length > 0, {timeout: 15000});
  const diff = await page.locator('#wfDiffOut').textContent();
  assert.match(diff, /Changed: [\w-]+ \(task\)/);
});

test('a schedule needs its own token, shows the next fire and can be removed', async () => {
  await page.locator('#wfTabSchedules').click();
  await page.waitForFunction(() => !document.getElementById('wfSchedulesEmpty').hidden
    || document.querySelectorAll('#wfSchedules .workflows-item').length > 0, {timeout: 20000});
  await page.locator('#wfSchWorkflow').selectOption(savedId);
  await page.locator('#wfSchKind').selectOption('interval');
  await page.locator('#wfSchMinutes').fill('30');
  await page.locator('#wfSchPreviewBtn').click();
  await page.waitForFunction(() => {
    const items = [...document.querySelectorAll('#wfSchPreview li')];
    return items.length > 0 && !items.every((item) => /Loading/.test(item.textContent));
  }, {timeout: 15000});
  const preview = await page.locator('#wfSchPreview').textContent();
  assert.match(preview, /in \d+ min/);
  await page.locator('#wfSchSave').click();
  await confirm('#wfSchConfirm', 'SCHEDULE WORKFLOW');
  const row = page.locator('#wfSchedules .workflows-item', {hasText: `${PREFIX}draft`});
  await row.waitFor({timeout: 20000});
  assert.match(await row.textContent(), /Next/);
  assert.match(await row.textContent(), /every 30 minutes/);

  await row.getByRole('button', {name: 'Disable', exact: true}).click();
  await confirm('#wfScheduleConfirm', `DISABLE SCHEDULE ${await scheduleId()}`);
  await page.waitForFunction((prefix) => {
    const card = [...document.querySelectorAll('#wfSchedules .workflows-item')].find((item) => item.textContent.includes(prefix));
    return !!card && /Nothing fires while it is disabled/.test(card.textContent);
  }, `${PREFIX}draft`, {timeout: 20000});
  assert.match(await row.textContent(), /Disabled/);

  await row.getByRole('button', {name: 'Enable', exact: true}).click();
  await confirm('#wfScheduleConfirm', `ENABLE SCHEDULE ${await scheduleId()}`);
  await page.waitForFunction((prefix) => {
    const card = [...document.querySelectorAll('#wfSchedules .workflows-item')].find((item) => item.textContent.includes(prefix));
    return !!card && /Next /.test(card.textContent.replace(/Next$/, ' Next'));
  }, `${PREFIX}draft`, {timeout: 20000});

  await row.getByRole('button', {name: 'Remove', exact: true}).click();
  await confirm('#wfScheduleConfirm', `REMOVE SCHEDULE ${await scheduleId()}`);
  await page.waitForFunction((prefix) => ![...document.querySelectorAll('#wfSchedules .workflows-item')]
    .some((item) => item.textContent.includes(prefix)), `${PREFIX}draft`, {timeout: 20000});
});

async function scheduleId() {
  const card = page.locator('#wfSchedules .workflows-item', {hasText: `${PREFIX}draft`}).first();
  const text = await card.textContent();
  const found = text.match(/w-[0-9a-f]{8}/);
  assert.ok(found, `schedule id missing from card: ${text}`);
  return found[0];
}

test('a real run shows each node with its status and evidence', async () => {
  await page.locator('#wfTabBuilder').click();
  await page.waitForFunction(() => document.getElementById('wfDraftSource').textContent.startsWith('From'), {timeout: 15000});
  await page.locator('#wfRunDraft').click();
  await confirm('#wfConfirmHost', 'RUN REVIEWED WORKFLOW');
  await page.waitForFunction(() => document.querySelectorAll('#wfRunDetail .workflows-runNode').length > 0, {timeout: 40000});
  await page.waitForFunction(() => {
    const rows = [...document.querySelectorAll('#wfRunDetail .workflows-runNode')];
    return rows.length > 0 && rows.every((row) => row.dataset.status && row.dataset.status !== 'pending');
  }, {timeout: 120000});
  const detail = await page.locator('#wfRunDetail').textContent();
  assert.match(detail, /evidence/);
  const statuses = await page.evaluate(() => [...document.querySelectorAll('#wfRunDetail .workflows-runNode')]
    .map((row) => row.dataset.status));
  assert.equal(statuses.every((status) => status === 'done'), true, `node statuses: ${statuses}`);
  assert.equal(await page.locator('#wfRuns .workflows-item').count() >= 1, true);
});

test('Bangla and a 360 px phone keep the page usable', async () => {
  await page.evaluate(() => window.nexusI18n.setLang('bn'));
  await page.waitForTimeout(400);
  assert.match(await page.locator('#wfTabSchedules').textContent(), /সময়সূচি/);
  assert.match(await page.locator('#wfNotes').textContent(), /[\u0980-\u09FF]/);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0);
  await page.setViewportSize({width: 360, height: 780});
  await page.waitForTimeout(500);
  await page.evaluate(() => window.nexusGui.show('workflows'));
  await page.waitForTimeout(300);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0);
  assert.equal(await page.locator('#wfTabBuilder').isVisible(), true);
  await page.evaluate(() => window.nexusI18n.setLang('en'));
  assert.deepEqual(errors, []);
});
