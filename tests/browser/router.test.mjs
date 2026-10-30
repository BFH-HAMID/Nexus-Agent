/** Prompt 4 acceptance: the Router Studio reads the real policy, previews a
 *  route without a model, saves a rule only after a typed confirmation, scores
 *  a lab batch offline, and shows what it cannot measure.
 *
 *  The suite leaves the workspace routing policy disabled and rule-free; every
 *  write goes through the same reviewed endpoints the UI uses. */
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
  const context = await browser.newContext({viewport: {width, height}, reducedMotion: 'reduce'});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(base + '/dashboard');
  await page.locator('#guiToken').fill(token);
  await page.locator('#guiLogin button').click();
  await page.waitForFunction(() => document.getElementById('metaUplink').textContent === 'online', {timeout: 20000});
  if (width > 900) await page.locator('.sidebar-nav [data-nav-id="router"]').click();
  else await page.evaluate(() => window.nexusGui.show('router'));
  await page.waitForSelector('#view-router .router-card', {timeout: 20000});
  await page.waitForFunction(() => document.getElementById('view-router').dataset.busy === 'false', {timeout: 20000});
  return {page, context, errors};
}

const api = (page, path, body) => page.evaluate(
  async ([p, b]) => (await window.nexusGui.api(p, b)).data, [path, body]);
const taskCount = (page) => page.evaluate(async () => (await window.nexusGui.api('/api/tasks')).length);

async function disableRules(page) {
  const rules = await api(page, '/api/router/rules');
  for (const rule of rules.rules) {
    await page.evaluate(async ({id, revision, confirmation}) => {
      await window.nexusGui.api('/api/router/rule', {action: 'remove', id, revision, confirmation});
    }, {id: rule.id, revision: rules.revision, confirmation: `REMOVE RULE ${rule.id}`});
  }
  const latest = await api(page, '/api/router/rules');
  if (latest.enabled) {
    await page.evaluate(async (revision) => {
      await window.nexusGui.api('/api/router/enable', {enabled: false, revision, confirmation: 'DISABLE RULES'});
    }, latest.revision);
  }
}

test('the policy tab reads the live settings, stamps its read and fits the page', async () => {
  const {page, context, errors} = await open();
  try {
    const overview = await api(page, '/api/router/overview');
    assert.equal(overview.policy.enabled, false);
    assert.equal(overview.settings.classifier_agent.length > 0, true);
    const facts = await page.locator('#rtFacts').innerText();
    assert.match(facts, new RegExp(String(overview.settings.router_min_score).replace('.', '\\.')));
    assert.ok(facts.includes(overview.settings.default_agent), 'default agent from agents.yaml');
    assert.match(await page.locator('#rtUpdated').innerText(), /^Read from the server at \d/);
    assert.match(await page.locator('#rtStats').innerText(), /^\d+ roles · \d+ agents · \d+ rules$/);
    assert.equal(await page.locator('#rtModelBody tr').count(), overview.roster.agents);
    assert.equal(await page.locator('.router-tile').count(), 0, 'tiles belong to other tabs');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('a rule is prepared, saved with a typed confirmation, used by the router and removed', async () => {
  const {page, context, errors} = await open();
  try {
    await disableRules(page);
    const role = 'backend';
    const phrase = `router-suite-${Date.now()}`;
    /* enable the master switch: review first, typed token, then the save */
    page.once('dialog', (dialog) => dialog.accept('ENABLE RULES'));
    await page.locator('#rtEnableRules').click();
    await page.waitForFunction(() => document.getElementById('rtEnableRules').textContent.includes('Disable'));
    assert.equal((await api(page, '/api/router/rules')).enabled, true);

    await page.locator('#rtTabRules').click();
    await page.fill('#rtRulePhrase', phrase);
    await page.fill('#rtRuleRole', role);
    await page.locator('#rtRuleSave').click();
    await page.waitForSelector('#rtRuleConfirmRow:not([hidden])');
    const confirmation = await page.locator('#rtRuleConfirm').getAttribute('placeholder');
    assert.match(confirmation, /^SAVE RULE r-[0-9a-f]{8}$/);
    assert.equal((await api(page, '/api/router/rules')).rules.length, 0, 'preparing must not write');
    await page.locator('#rtRuleConfirm').fill(confirmation);
    await page.locator('#rtRuleConfirmGo').click();
    await page.waitForFunction(() => document.getElementById('rtRuleStatus').dataset.tone === 'ok');
    await page.waitForSelector('#rtRuleList .router-rule');
    const saved = await api(page, '/api/router/rules');
    assert.equal(saved.rules.length, 1);
    assert.equal(saved.rules[0].phrase, phrase);

    const preview = await api(page, '/api/router/simulate', {task: phrase});
    assert.equal(preview.provider_calls, 0);
    assert.equal(preview.decision.role, role);
    assert.ok(preview.rules_matched.some((rule) => rule.phrase === phrase));

    const id = saved.rules[0].id;
    page.once('dialog', (dialog) => dialog.accept(`REMOVE RULE ${id}`));
    await page.locator('#rtRuleList .router-rule .btn.danger').click();
    await page.waitForFunction(() => document.getElementById('rtRuleStatus').textContent.includes('removed'));
    assert.equal((await api(page, '/api/router/rules')).rules.length, 0);
    await page.locator('#rtTabPolicy').click();
    page.once('dialog', (dialog) => dialog.accept('DISABLE RULES'));
    await page.locator('#rtEnableRules').click();
    await page.waitForFunction(() => document.getElementById('rtEnableRules').textContent.includes('Enable'));
    assert.equal((await api(page, '/api/router/rules')).enabled, false);
    assert.deepEqual(errors, []);
  } finally {
    await disableRules(page).catch(() => {});
    await context.close();
  }
});

test('a draft rule can be dry-run without being saved and without a model call', async () => {
  const {page, context, errors} = await open();
  try {
    await disableRules(page);
    await page.locator('#rtTabRules').click();
    const phrase = 'draft-only-orchestration';
    await page.fill('#rtRulePhrase', phrase);
    await page.fill('#rtRuleRole', 'backend');
    await page.fill('#rtPreviewTask', phrase);
    await page.locator('#rtPreviewGo').click();
    await page.waitForFunction(() => document.getElementById('rtPreviewChip').textContent === 'matched');
    const out = await page.locator('#rtPreviewOut').innerText();
    assert.match(out, /backend/);
    assert.match(out, /0 provider calls/);
    assert.equal((await api(page, '/api/router/rules')).rules.length, 0, 'a preview never writes');
    assert.equal((await api(page, '/api/router/rules')).enabled, false);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('every fallback ladder renders with its real hazards and counts', async () => {
  const {page, context, errors} = await open();
  try {
    await page.locator('#rtTabChains').click();
    await page.waitForSelector('#rtChains .router-chain');
    const chains = await api(page, '/api/router/chains');
    assert.equal(await page.locator('#rtChains .router-chain').count(), chains.agents.length);
    assert.ok(chains.agents.length >= 13, 'the shipped roster has 13 agents');
    for (const chain of chains.agents) {
      assert.ok(chain.nodes.length >= 1, chain.agent + ' has no node');
    }
    const tiles = await page.locator('#rtChainTiles').innerText();
    assert.ok(tiles.includes(String(chains.summary.broken)), 'broken-link count is shown');
    assert.ok(tiles.includes(String(chains.summary.missing_keys)), 'missing-credential count is shown');
    const first = page.locator('#rtChains .router-chain').first();
    assert.ok(await first.locator('.router-node').count() >= 2, 'a ladder shows its hops');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('the lab scores expected roles offline, records the run and starts no task', async () => {
  const {page, context, errors} = await open();
  try {
    const task = 'build a python api with docker and postgres';
    const simulated = await api(page, '/api/router/simulate', {task});
    assert.ok(simulated.decision, 'the curated roster routes this task');
    const before = await taskCount(page);
    await page.locator('#rtTabLab').click();
    await page.fill('#rtLabCases', `${task} => ${simulated.decision.role}\nqqqxxzz blorptastic => ${simulated.decision.role}`);
    await page.locator('#rtLabRun').click();
    await page.waitForFunction(() => document.getElementById('rtLabStatus').dataset.tone === 'ok', {timeout: 15000});
    const summary = await page.locator('#rtLabSummary').innerText();
    assert.match(summary, /50%/);
    assert.match(summary, /0 provider calls/);
    assert.equal(await page.locator('#rtLabBody tr').count(), 2);
    const run = (await api(page, '/api/router/lab')).runs[0];
    assert.equal(run.accuracy, 0.5);
    assert.equal(run.cases[0].pass, true);
    assert.equal(run.provider_calls, 0);
    assert.ok(await page.locator('#rtLabHistory li').count() >= 1);
    assert.equal(await taskCount(page), before, 'a lab run never creates a task');
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('the accuracy panel shows explicit feedback, real runs and its own blind spots', async () => {
  const {page, context, errors} = await open();
  try {
    await page.locator('#rtTabAccuracy').click();
    await page.waitForSelector('#rtAccUnknown li');
    const data = await api(page, '/api/router/accuracy');
    assert.equal(await page.locator('#rtAccTiles .router-tile').count(), 5);
    assert.equal(await page.locator('#rtAccChecked li').count(), data.checked.length);
    assert.equal(await page.locator('#rtAccUnknown li').count(), data.not_checked.length);
    const unknown = await page.locator('#rtAccUnknown').innerText();
    assert.match(unknown, /classifier/i);
    assert.match(unknown, /not (recorded|verification)|no independent|only whole-session|not billed/i);
    const tiles = await page.locator('#rtAccTiles').innerText();
    assert.ok(tiles.includes(String(data.feedback.observations)));
    assert.ok(tiles.includes(String(data.totals.runs)));
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('the composer previews a route offline and names a forced role', async () => {
  const {page, context, errors} = await open();
  try {
    await page.evaluate(() => window.nexusGui.show('run'));
    await page.waitForSelector('#routePreviewBtn');
    await page.waitForFunction(() => [...document.querySelectorAll('#roleSelect option')]
      .some((option) => option.value === 'backend'), {timeout: 15000});
    const task = 'build a python api with docker and postgres';
    const before = await taskCount(page);
    await page.fill('#taskInput', task);
    await page.locator('#routePreviewBtn').click();
    await page.waitForFunction(() => document.getElementById('routePreview').textContent.includes('→'));
    const text = await page.locator('#routePreview').innerText();
    const simulated = await api(page, '/api/router/simulate', {task});
    assert.ok(text.includes(simulated.decision.role), text);
    assert.match(text, /score/);
    assert.equal(await taskCount(page), before, 'a preview never runs');
    /* choosing a role in the existing override shows the forced decision */
    await page.selectOption('#roleSelect', 'backend');
    await page.locator('#routePreviewBtn').click();
    await page.waitForFunction(() => document.getElementById('routePreview').textContent.includes('Forced role'));
    assert.match(await page.locator('#routePreview').innerText(), /Forced role: backend/);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('Bangla covers every new label and the studio fits a 360 px phone', async () => {
  const {page, context, errors} = await open(360, 740);
  try {
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0);
    assert.equal(await page.locator('#view-router h1').isVisible(), true);
    assert.deepEqual(await page.evaluate(() => window.nexusI18n.missing()), []);
    await page.evaluate(() => window.nexusI18n.setLang('bn'));
    assert.match(await page.locator('#view-router h1').innerText(), /রোলে/);
    assert.match(await page.locator('#rtTabPolicy').innerText(), /পলিসি/);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0);
    await page.locator('#rtTabLab').click();
    assert.equal(await page.locator('#rtLabCases').isVisible(), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0);
    await page.evaluate(() => window.nexusI18n.setLang('en'));
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});
