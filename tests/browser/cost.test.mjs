/** Prompt 10 acceptance: Cost & performance over the real server.
 *
 *  Nothing here is a mock. The tabs read the live ledger, the budget is saved
 *  through the real typed-token sheet, and the resource cards are the numbers
 *  this machine reported a moment ago. The suite is deliberately unable to
 *  spend money: it opens the comparison confirmation and cancels it, and it
 *  puts the budget back to the value it found before it finishes.
 */
import {test, before, after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import bundled from '@sparticuz/chromium';

const base = process.env.NEXUS_TEST_URL || 'http://127.0.0.1:8787';
const token = process.env.NEXUS_GUI_TEST_TOKEN;
let browser;
let context;
let page;
let errors = [];
let original;

const headers = () => ({'X-Nexus-Token': token, 'Content-Type': 'application/json'});
async function api(route, body, attempt = 0) {
  const reply = await fetch(base + route, {
    method: body === undefined ? 'GET' : 'POST', headers: headers(),
    ...(body === undefined ? {} : {body: JSON.stringify(body)}),
  });
  const payload = await reply.json();
  if (reply.status === 429 && attempt === 0) {
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
    await new Promise((done) => setTimeout(done, 900));
  }
}

async function signIn(target) {
  await target.goto(base + '/dashboard');
  await target.locator('#guiToken').fill(token);
  await target.locator('#guiLogin button').click();
  await target.waitForFunction(() => document.getElementById('metaUplink').textContent === 'online', {timeout: 20000});
}

async function openCost(tab = 'overview') {
  await page.locator('.sidebar-nav [data-nav-id="cost"]').click();
  await page.locator(`[data-cost-tab="${tab}"]`).click();
  await page.waitForFunction((name) => !document.querySelector(`[data-cost-panel="${name}"]`)?.hidden, tab, {timeout: 15000});
}

async function confirmWord(word) {
  await page.locator('#costActionHost #costActionToken').waitFor({timeout: 10000});
  await page.locator('#costActionHost #costActionToken').fill(word);
  assert.equal(await page.locator('#costActionHost #costActionGo').isDisabled(), false,
    'the confirm button stays disabled until the token matches');
  await page.locator('#costActionHost #costActionGo').click();
}

before(async () => {
  assert.ok(token, 'Set NEXUS_GUI_TEST_TOKEN for the live dashboard');
  original = await api('/api/cost/budget');
  browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || await bundled.executablePath(),
    args: bundled.args.filter((a) => !['--single-process', '--disable-web-security'].includes(a)),
    headless: true,
  });
  context = await browser.newContext({viewport: {width: 1600, height: 1000}, reducedMotion: 'reduce'});
  page = await context.newPage();
  page.on('pageerror', (error) => errors.push(error.message));
  await signIn(page);
  await openCost('overview');
  await page.waitForFunction(() => document.querySelectorAll('#costKpis .security-count').length === 6, {timeout: 20000});
});

after(async () => {
  // Restore the policy this suite found, so a test run leaves no budget behind.
  try {
    if (original) {
      const view = await api('/api/cost/policy');
      await api('/api/cost/policy/save', {
        confirmation: 'SAVE COST POLICY', revision: view.revision,
        budget: {enabled: original.enabled, stop: original.stop,
          unknown_price_blocks: original.unknown_price_blocks,
          daily_usd: original.daily_usd, monthly_usd: original.monthly_usd},
      });
    }
  } catch (error) {
    errors.push('restore failed: ' + error.message);
  }
  await context?.close();
  await browser?.close();
});

test('the page opens on the real ledger, with its provenance on screen', async () => {
  const overview = await api('/api/cost/overview');
  const state = await page.evaluate(() => window.nexusCost.state());
  assert.equal(state.enabled, overview.budget.enabled);
  assert.equal(state.spendToday, overview.spend.today.usd);
  assert.equal(state.calls, overview.spend.today.calls);
  assert.equal(await page.locator('#view-cost [data-cost-tab]').count(), 6);
  // The store line names the real file, or says plainly that there is none yet.
  const line = await page.locator('#costStoreLine').textContent();
  assert.ok(line.includes('cost.db') || line.includes('No ledger'), line);
  // The chip repeats the budget state instead of a friendly summary.
  assert.match(await page.locator('#costChip').textContent(), /Budget/);
  assert.doesNotMatch(await page.locator('#costUpdated').textContent(), /^$/);
  assert.equal(errors.length, 0, errors.join('\n'));
});

test('each tab loads its own screen and nothing throws', async () => {
  for (const tab of ['latency', 'breakdown', 'budget', 'compare', 'resources', 'overview']) {
    await openCost(tab);
    if (tab === 'latency') {
      await page.waitForFunction(() => document.getElementById('costPercentiles').children.length >= 3, {timeout: 15000});
      const meta = await page.locator('#costLatencyMeta').textContent();
      assert.match(meta, /day window/i);
    }
    if (tab === 'breakdown') {
      await page.waitForFunction(() => document.querySelector('#costBreakdownRows')?.children.length >= 1, {timeout: 15000});
      assert.match(await page.locator('#costBreakdownMeta').textContent(), /call/i);
    }
    if (tab === 'budget') {
      await page.waitForFunction(() => document.querySelectorAll('#costBudgetFacts .security-fact, #costBudgetFacts .memory-fact').length >= 1
        || document.getElementById('costBudgetFacts').children.length >= 1, {timeout: 15000});
      const checked = await page.locator('#costBudgetEnabled').isChecked();
      assert.equal(checked, (await api('/api/cost/budget')).enabled);
    }
    if (tab === 'compare') {
      await page.waitForFunction(() => document.querySelectorAll('#costModelList input[type="checkbox"]').length > 0, {timeout: 15000});
      const listed = await page.locator('#costModelList input[type="checkbox"]').count();
      assert.equal(listed, (await api('/api/cost/providers')).agents.length);
    }
    assert.equal(await page.locator(`[data-cost-panel="${tab}"]`).isVisible(), true);
  }
  assert.equal(errors.length, 0, errors.join('\n'));
});

test('a budget is only saved with the typed token, and it changes the API', async () => {
  await openCost('budget');
  // Wait for the tab to paint the stored value before typing over it, or a late
  // repaint would put the old number back between the fill and the save.
  await page.waitForFunction((want) => document.getElementById('costDaily').value === String(want),
    original.daily_usd, {timeout: 15000});
  const daily = original.daily_usd === 2.5 ? 3.25 : 2.5;
  await page.locator('#costDaily').fill(String(daily));
  await page.locator('#costBudgetSave').click();
  await page.locator('#costActionHost #costActionToken').waitFor({timeout: 10000});
  // The sheet shows the token it wants; the button is dead until it matches.
  assert.equal(await page.locator('#costActionExpected').textContent(), 'SAVE COST POLICY');
  assert.equal(await page.locator('#costActionGo').isDisabled(), true);
  await page.locator('#costActionCancel').click();
  assert.equal((await api('/api/cost/budget')).daily_usd, original.daily_usd, 'cancelling saves nothing');

  await page.locator('#costBudgetSave').click();
  await confirmWord('SAVE COST POLICY');
  let saved;
  try {
    saved = await waitForApi(async () => {
      const value = await api('/api/cost/budget');
      return value.daily_usd === daily ? value : null;
    });
  } catch (error) {
    const sheet = await page.locator('#costActionError').count()
      ? await page.locator('#costActionError').textContent() : '(closed)';
    const now = await api('/api/cost/budget');
    const input = await page.locator('#costDaily').inputValue();
    throw new Error(`${error.message}; sheet=${JSON.stringify(sheet)} api=${now.daily_usd} input=${input} want=${daily} original=${original.daily_usd}`);
  }
  assert.equal(saved.daily_usd, daily);
  const want = daily.toFixed(2);
  const chip = await waitForApi(async () => {
    const text = await page.locator('#costChip').textContent();
    return text.includes(`$${want}`) ? text : null;
  }, 15000).catch(async (error) => {
    throw new Error(`${error.message}; chip=${JSON.stringify(await page.locator('#costChip').textContent())} want=$${want}`);
  });
  assert.ok(chip.includes(`$${want}`));
  assert.equal(await page.evaluate(() => window.nexusCost.state().enabled), saved.enabled);
});

test('a comparison needs choices and a prompt, and never runs without confirmation', async () => {
  await openCost('compare');
  await page.locator('#costCompareRun').click();
  await page.waitForFunction(() => !document.getElementById('costError').hidden, {timeout: 10000});
  assert.match(await page.locator('#costError').textContent(), /Select 2 to 4 models/);

  const boxes = page.locator('#costModelList input[type="checkbox"]');
  await boxes.nth(0).check();
  await boxes.nth(1).check();
  await page.locator('#costPrompt').fill('Which claim in this workspace is not backed by a file?');
  await page.locator('#costCompareRun').click();
  await page.locator('#costActionHost #costActionExpected').waitFor({timeout: 10000});
  assert.equal(await page.locator('#costActionExpected').textContent(), 'START COMPARISON');
  // Read the real names off the sheet: the confirmation must name the models.
  const detail = await page.locator('#costActionHost .security-note').first().textContent();
  const providers = await api('/api/cost/providers');
  for (const name of [providers.agents[0].name, providers.agents[1].name]) {
    assert.ok(detail.includes(name), `${name} is named before the call is made`);
  }
  await page.locator('#costActionCancel').click();
  assert.equal(await page.evaluate(() => window.nexusCost.state().job), null, 'cancelling starts no job');
  assert.equal(await page.locator('#costActionHost').isVisible(), false);
  // The selection is page state, never a request: the stamp still names it.
  assert.match(await page.locator('#costCompareState').textContent(), /2 selected/);
});

test('the resource tab shows this machine, measured now', async () => {
  await openCost('resources');
  await page.waitForFunction(() => document.querySelectorAll('#costResourceKpis .security-count').length === 4, {timeout: 15000});
  const data = await waitForApi(async () => {
    const body = await api('/api/cost/resources');
    return body.history.length ? body : null;
  });
  const latest = data.history[data.history.length - 1];
  assert.ok(latest.cpu !== null && latest.cpu !== undefined, 'a real CPU number, not a placeholder');
  // The page and this probe sample independently, so compare against the last
  // few real measurements instead of demanding they be the same instant.
  const shown = await page.locator('#costResourceKpis .security-count').nth(0).locator('b').textContent();
  const recent = data.history.slice(-4).map((row) => `${Number(row.cpu).toFixed(1)}%`);
  assert.ok(recent.includes(shown), `showed ${shown}; the ledger holds ${recent.join(', ')}`);
  await page.waitForFunction(() => document.querySelector('#costResourceRows tr') !== null, {timeout: 15000});
  assert.match(await page.locator('#costResourceCount').textContent(), /sample/i);
  assert.equal(errors.length, 0, errors.join('\n'));
});

test('the page survives a language switch and a 360 px viewport', async () => {
  await openCost('overview');
  await page.evaluate(() => window.nexusI18n.setLang('bn'));
  await page.waitForFunction(() => document.documentElement.lang === 'bn', {timeout: 10000});
  assert.match(await page.locator('#view-cost h1').textContent(), /[\u0980-\u09FF]/);
  assert.match(await page.locator('[data-cost-tab="budget"]').textContent(), /[\u0980-\u09FF]/);
  await page.evaluate(() => window.nexusI18n.setLang('en'));
  await page.waitForFunction(() => document.documentElement.lang === 'en', {timeout: 10000});
  assert.match(await page.locator('#view-cost h1').textContent(), /spent/i);
  await page.setViewportSize({width: 360, height: 760});
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  assert.ok(overflow <= 1, `horizontal overflow of ${overflow}px at 360px`);
  await page.locator('[data-cost-tab="resources"]').click();
  await page.waitForFunction(() => !document.querySelector('[data-cost-panel="resources"]')?.hidden, {timeout: 10000});
  const narrow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  assert.ok(narrow <= 1, `horizontal overflow of ${narrow}px on the resources tab`);
  await page.setViewportSize({width: 1600, height: 1000});
  assert.equal(errors.length, 0, errors.join('\n'));
});
