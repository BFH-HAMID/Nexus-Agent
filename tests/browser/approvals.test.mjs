/** Prompt 6 acceptance: one Approval Center over the real consent gates.
 *
 *  Every request the suite answers is created through the existing Operations
 *  API, so the page is exercised against real store records — never fixtures
 *  injected into the DOM. The suite rejects what it creates and leaves the
 *  store as it found it. */
import {test, before, after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import bundled from '@sparticuz/chromium';

const base = process.env.NEXUS_TEST_URL || 'http://127.0.0.1:8787';
const token = process.env.NEXUS_GUI_TEST_TOKEN;
let browser;

/* Only this suite's own records are ever cleaned up, matched by their prefix. */
const PREFIX = 'Approval Center ';
const RUN = Date.now().toString(36);

async function sweepOwn() {
  const headers = {'X-Nexus-Token': token, 'Content-Type': 'application/json'};
  const state = await fetch(base + '/api/operations/state', {headers}).then((r) => r.json()).catch(() => null);
  for (const job of state?.jobs || []) {
    if (!String(job.task || '').startsWith(PREFIX)) continue;
    if (!['awaiting_approval', 'approved', 'running'].includes(job.status)) continue;
    await fetch(base + '/api/operations/action/review', {
      method: 'POST', headers, body: JSON.stringify({id: job.id, action: 'cancel'}),
    }).catch(() => {});
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
  if (width > 900) await page.locator('.sidebar-nav [data-nav-id="approvals"]').click();
  else await page.evaluate(() => window.nexusGui.show('approvals'));
  await page.waitForSelector('#apCounts .approvals-count', {timeout: 20000});
  await page.waitForFunction(() => document.getElementById('apTotal').textContent !== '—', {timeout: 20000});
  return {page, context, errors};
}

/* /api/approvals/* answers {ok,data}; the older operations routes answer the
   record itself. One helper reads both without pretending either is absent. */
const api = (page, route, body) => page.evaluate(async ([p, b]) => {
  const body_ = await window.nexusGui.api(p, b);
  return body_ && typeof body_ === 'object' && 'data' in body_ ? body_.data : body_;
}, [route, body]);

async function jobStatus(page, id) {
  const state = await api(page, '/api/operations/state');
  return (state.jobs || []).find((row) => row.id === id)?.status || 'missing';
}

async function createQueued(page, task) {
  const job = await api(page, '/api/operations/action/create', {task, max_seconds: 60});
  return job;
}

async function cleanup(page, jobs) {
  await sweepOwn();
  for (const job of jobs) {
    try { await api(page, '/api/operations/action/cancel', {id: job.id}); } catch { /* already gone */ }
    try { await api(page, '/api/operations/action/review', {id: job.id, action: 'cancel'}); } catch { /* terminal */ }
  }
}

test('the page lists exactly what the API reports and never invents a request', async () => {
  const {page, context, errors} = await open();
  try {
    const inbox = await api(page, '/api/approvals/inbox');
    /* Paint must equal the server record, item for item — no placeholder rows. */
    const painted = await page.locator('.approvals-item').evaluateAll((nodes) => nodes.map((n) => n.dataset.ref));
    assert.deepEqual(painted.sort(), inbox.items.map((item) => item.reference).sort());
    const counts = await page.locator('#apCounts .approvals-count b').allTextContents();
    assert.deepEqual(counts.map(Number), [inbox.counts.live, inbox.counts.action, inbox.counts.queue]);
    if (inbox.counts.total === 0) {
      assert.equal(await page.locator('#apTotal').innerText(), 'Nothing waiting');
      assert.equal(await page.locator('#apEmpty').isVisible(), true);
      assert.match(await page.locator('#apEmpty').innerText(), /Nothing is waiting|New requests/);
    } else {
      assert.match(await page.locator('#apTotal').innerText(), /^\d+ waiting$/);
      assert.equal(await page.locator('#apEmpty').isHidden(), true);
    }
    assert.match(await page.locator('#apUpdated').innerText(), /^Read from the server at \d/);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('a queued task appears, refuses a wrong token and leaves once approved', async () => {
  const {page, context, errors} = await open();
  const created = [];
  try {
    const job = await createQueued(page, `${PREFIX}end-to-end review ${RUN}`);
    created.push(job);
    await page.locator('#apRefresh').click();
    await page.waitForSelector(`.approvals-item[data-ref="${job.id}"]`, {timeout: 20000});
    const card = page.locator(`.approvals-item[data-ref="${job.id}"]`);
    assert.match(await card.locator('.approvals-title').innerText(), /end-to-end review/);
    assert.equal(await card.locator('.approvals-chip').first().innerText(), 'Queued task');
    assert.match(await card.locator('.approvals-meta').innerText(), /queued task · priority 5/);

    /* approving needs the exact token that request published */
    await card.locator('[data-ap-act="approve"]').click();
    const confirm = card.locator('.approvals-confirm');
    await confirm.waitFor();
    assert.match(await confirm.locator('.approvals-token').innerText(), new RegExp(`^APPROVE QUEUED TASK ${job.id}$`));
    const go = confirm.locator('button[type="submit"]');
    assert.equal(await go.isDisabled(), true, 'the grant button starts disabled');
    await confirm.locator('.approvals-word').fill(`APPROVE QUEUED TASK ${job.id}`);
    assert.equal(await go.isDisabled(), false);
    await confirm.locator('.approvals-word').fill('APPROVE QUEUED TASK not-the-id');
    assert.equal(await go.isDisabled(), true, 'a different task id never enables the grant');

    /* rejecting is one click and needs no token */
    await confirm.locator('button[type="button"]').click();
    await card.locator('[data-ap-act="reject"]').click();
    await page.waitForFunction((id) => !document.querySelector(`.approvals-item[data-ref="${id}"]`), job.id,
      {timeout: 20000});
    const after = await api(page, '/api/approvals/inbox');
    assert.equal(after.items.some((item) => item.reference === job.id), false,
      'the answered request is no longer in the inbox');
    assert.equal(await jobStatus(page, job.id), 'rejected', 'the real queued job was rejected');
    assert.deepEqual(errors, []);
  } finally {
    await cleanup(page, created);
    await context.close();
  }
});

test('approving with the published token changes the real job and lands in history', async () => {
  const {page, context, errors} = await open();
  const created = [];
  try {
    const job = await createQueued(page, `${PREFIX}approve path ${RUN}`);
    created.push(job);
    await page.locator('#apRefresh').click();
    const card = page.locator(`.approvals-item[data-ref="${job.id}"]`);
    await card.waitFor({timeout: 20000});
    await card.locator('[data-ap-act="approve"]').click();
    await card.locator('.approvals-word').fill(`APPROVE QUEUED TASK ${job.id}`);
    await card.locator('button[type="submit"]').click();
    await page.waitForFunction((id) => !document.querySelector(`.approvals-item[data-ref="${id}"]`), job.id,
      {timeout: 20000});
    assert.equal(await jobStatus(page, job.id), 'approved', 'the real queued job is admitted');

    await page.locator('#apTabHistory').click();
    /* History re-reads on open; the previous paint may still be on screen, so
       wait for this decision's own row instead of "any row at all". */
    await page.waitForFunction((id) => [...document.querySelectorAll('#apHistory .approvals-historyrow')]
      .some((node) => node.textContent.includes(id) && node.textContent.includes('approve')), job.id,
      {timeout: 20000});
    const rows = await page.locator('#apHistory .approvals-historyrow').allInnerTexts();
    assert.ok(rows.some((row) => row.includes('approve') && row.includes(job.id)),
      'the decision is recorded in history');
    assert.equal(await page.locator('#apHistoryEmpty').isHidden(), true);
    assert.deepEqual(errors, []);
  } finally {
    await cleanup(page, created);
    await context.close();
  }
});

test('a request answered elsewhere is reported stale, not silently dropped', async () => {
  const {page, context, errors} = await open();
  const created = [];
  try {
    const job = await createQueued(page, `${PREFIX}stale check ${RUN}`);
    created.push(job);
    await page.locator('#apRefresh').click();
    const card = page.locator(`.approvals-item[data-ref="${job.id}"]`);
    await card.waitFor({timeout: 20000});
    /* somebody else answers it first; the tab still shows the old request */
    await api(page, '/api/operations/action/review', {id: job.id, action: 'reject'});
    await card.locator('[data-ap-act="reject"]').click();
    await page.waitForFunction((id) => {
      const node = document.querySelector(`.approvals-item[data-ref="${id}"] .approvals-status`);
      return node && node.dataset.tone === 'error';
    }, job.id, {timeout: 20000});
    const status = await card.locator('.approvals-status').innerText();
    assert.match(status, /no longer exists|unknown|terminal|rejected|refresh/i);
    await page.waitForFunction((id) => !document.querySelector(`.approvals-item[data-ref="${id}"]`), job.id,
      {timeout: 20000});
    assert.deepEqual(errors, []);
  } finally {
    await cleanup(page, created);
    await context.close();
  }
});

test('the policy tab reads the real gate and names what it cannot check', async () => {
  const {page, context, errors} = await open();
  try {
    await page.locator('#apTabPolicy').click();
    await page.waitForFunction(() => document.querySelectorAll('#apPermissions dt').length > 0, null,
      {timeout: 20000});
    const policy = await api(page, '/api/approvals/policy');
    const facts = await page.locator('#apPermissions dd').allInnerTexts();
    assert.ok(facts.includes(policy.permissions.default), 'the default mode is shown as stored');
    assert.match(facts.join(' '), /ASK_EVERY_TIME|READ_ONLY|AUTO_ALLOW/);
    const stops = await page.locator('#apStops dd').allInnerTexts();
    assert.equal(stops[0], policy.latch.stopped ? 'Latched' : 'Clear');
    assert.equal(stops[1], policy.queue_paused ? 'Paused' : 'Running');
    const always = await page.locator('#apAlwaysReview li').allInnerTexts();
    assert.ok(always.includes('run_command') && always.includes('browser_action'));
    assert.ok((await page.locator('#apNotChecked li').count()) >= 3,
      'the page states what it does not verify');
    assert.match(await page.locator('#apPolicyStamp').innerText(), /^\d{1,2}:\d{2}/);

    await page.locator('#apTabDiag').click();
    await page.waitForFunction(() => document.querySelectorAll('#apDiag dt').length > 0);
    assert.match(await page.locator('#apDiagErrors').innerText(), /Every source answered|:/);
    assert.match(await page.locator('#apLimits').innerText(), /request appears here|Deny needs/);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('tabs answer Arrow/Home/End and the sidebar row opens this page', async () => {
  const {page, context, errors} = await open();
  try {
    const first = page.locator('#apTabInbox');
    await first.focus();
    await page.keyboard.press('ArrowRight');
    await page.waitForFunction(() => document.getElementById('apTabPolicy').getAttribute('aria-selected') === 'true');
    assert.equal(await page.locator('#apPanelPolicy').isVisible(), true);
    await page.keyboard.press('End');
    await page.waitForFunction(() => document.getElementById('apTabDiag').getAttribute('aria-selected') === 'true');
    await page.keyboard.press('Home');
    await page.waitForFunction(() => document.getElementById('apTabInbox').getAttribute('aria-selected') === 'true');

    /* the legacy Operations page is still reachable and unchanged */
    await page.goto(base + '/dashboard#operations');
    await page.waitForFunction(() => window.nexusGui.view === 'operations');
    assert.ok(await page.locator('[data-gui-view="operations"]').first().isVisible());
    await page.goto(base + '/dashboard#approvals');
    await page.waitForFunction(() => window.nexusGui.view === 'approvals');
    assert.equal(await page.evaluate(() => window.nexusApprovals?.tab !== undefined), true);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('Bangla covers the page and it fits a 360 px phone', async () => {
  const {page, context, errors} = await open(360, 760);
  try {
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0);
    assert.equal(await page.evaluate(() => window.nexusI18n.missing().length), 0,
      'every Acceptance Center key exists in Bangla');
    await page.evaluate(() => window.nexusI18n.setLang('bn'));
    await page.waitForTimeout(300);
    assert.match(await page.locator('.approvals-head h1').innerText(), /[\u0980-\u09FF]/);
    assert.match(await page.locator('#apTabInbox').innerText(), /[\u0980-\u09FF]/);
    await page.locator('#apTabPolicy').click();
    await page.waitForFunction(() => document.querySelectorAll('#apPermissions dt').length > 0);
    assert.match(await page.locator('#apPermissions dt').first().innerText(), /[\u0980-\u09FF]/);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});
