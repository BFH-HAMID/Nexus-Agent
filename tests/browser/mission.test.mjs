/** Prompt 2 acceptance: Mission Control reads one summary endpoint and follows
 *  the five-event global feed; it is honest when a source is missing and it
 *  degrades to polling instead of pretending to be live. */
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
  const summaries = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('response', async (response) => {
    if (new URL(response.url()).pathname === '/api/mission/summary') summaries.push(response.status());
  });
  await page.goto(base + '/dashboard');
  await page.locator('#guiToken').fill(token);
  await page.locator('#guiLogin button').click();
  await page.waitForFunction(() => document.getElementById('metaUplink').textContent === 'online', {timeout: 20000});
  assert.equal(await page.evaluate(() => window.nexusGui.view), 'mission');
  return {page, context, errors, summaries};
}

async function live(page, timeout = 20000) {
  await page.waitForFunction(() => document.getElementById('msStream')?.dataset.state === 'live', null, {timeout});
}

test('one summary request paints every tile, and every value says when it was read', async () => {
  const {page, context, errors, summaries} = await open();
  try {
    await page.waitForFunction(() => document.getElementById('msDaemonState').textContent !== '—');
    const data = await page.evaluate(async () => (await window.nexusGui.api('/api/mission/summary')).data);

    assert.equal(data.schema, 1);
    assert.ok(data.observed_at > 0);
    for (const key of ['daemon', 'tasks', 'approvals', 'resources', 'usage', 'agents', 'activity', 'health', 'alerts']) {
      assert.ok(key in data, 'missing summary section: ' + key);
    }
    assert.equal(data.agents.reachability_checked, false, 'reachability is never inferred');
    assert.ok(data.usage.cost_reason, 'a missing price explains itself');
    assert.ok(data.resources.gpu_reason, 'a missing GPU says why');

    /* every tile shows a number or the explicit word for "we do not know" */
    const tiles = await page.evaluate(() => ({
      daemon: document.getElementById('msDaemonState').textContent.trim(),
      age: document.getElementById('msDaemonAge').textContent.trim(),
      active: document.getElementById('msTasksActive').textContent.trim(),
      cpu: document.getElementById('msCpu').textContent.trim(),
      gpu: document.getElementById('msGpu').textContent.trim(),
      agents: document.getElementById('msAgents').textContent.trim(),
      updated: document.getElementById('missionUpdated').textContent.trim(),
    }));
    assert.ok(tiles.daemon.length > 0 && tiles.daemon !== '—');
    assert.match(tiles.age, /\d+s ago|ago/, 'the daemon tile states the age of its reading');
    assert.match(tiles.active, /^\d+$/);
    assert.match(tiles.cpu, /^\d+%$|Unavailable/);
    assert.match(tiles.agents, /^\d+ \/ \d+$/);
    assert.match(tiles.updated, /Read from the server at \d/);
    assert.match(tiles.gpu, /^GPU: ./);
    assert.ok(summaries.length >= 1 && summaries.every((code) => code === 200), 'summary ' + summaries.join(','));
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('the global feed opens once, names its five events and survives a tab switch', async () => {
  const {page, context, errors} = await open();
  try {
    await live(page);
    const state = await page.evaluate(() => window.nexusMission.state());
    assert.equal(state.stream, 'live');
    assert.equal(await page.locator('#msStream [data-i18n]').getAttribute('data-i18n'), 'mission.stream.live');
    assert.equal(await page.locator('#sbSse').innerText(), 'SSE · live');

    /* the feed is global: leave the page and the stream must close again */
    await page.locator('.sidebar-nav [data-nav-id="run"]').click();
    await page.waitForFunction(() => window.nexusGui.view === 'run');
    await page.waitForFunction(() => document.getElementById('sbSse').textContent.trim() === 'SSE · idle', null, {timeout: 10000});
    await page.locator('.sidebar-nav [data-nav-id="mission"]').click();
    await live(page);

    /* A second listener on the same endpoint must get its own copy of the
       opening frames, each carrying the timestamp the page prints. */
    const frame = await page.evaluate(() => new Promise((resolve) => {
      const es = new EventSource(window.nexusMission.streamUrl());
      const payloads = {};
      const done = () => { es.close(); resolve(payloads); };
      for (const name of ['heartbeat', 'task.update', 'approval.new', 'metrics.tick']) {
        es.addEventListener(name, (event) => {
          payloads[name] = JSON.parse(event.data);
          if (Object.keys(payloads).length === 4) done();
        });
      }
      window.setTimeout(done, 12000);
    }));
    assert.deepEqual(Object.keys(frame).sort(), ['approval.new', 'heartbeat', 'metrics.tick', 'task.update']);
    for (const [name, payload] of Object.entries(frame)) {
      assert.ok(payload.observed_at > 0, name + ' carries a timestamp');
      assert.equal(payload.schema, 1);
    }
    assert.equal(frame.heartbeat.subscribers, 2, 'the page feed plus this listener');
    assert.equal(frame['approval.new'].state, 'ok');
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('metrics tick without a reload and the alert path is wired to the feed', async () => {
  const {page, context, errors} = await open();
  try {
    await live(page);
    const first = await page.locator('#missionUpdated').getAttribute('data-observed');
    const firstCpu = await page.locator('#msCpu').innerText();
    /* the sampler pushes at least one metrics.tick within the test window */
    await page.waitForFunction((before) => {
      const node = document.getElementById('missionUpdated');
      return node.dataset.observed && node.dataset.observed !== before;
    }, first, {timeout: 20000});
    const second = await page.locator('#missionUpdated').getAttribute('data-observed');
    assert.notEqual(second, first);
    assert.match(await page.locator('#msCpu').innerText(), /^\d+%$|Unavailable/, 'cpu before ' + firstCpu);
    /* no page reload happened: the palette state and the view are intact */
    assert.equal(await page.evaluate(() => window.nexusGui.view), 'mission');
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('a blocked stream degrades to polling and says so instead of claiming live', async () => {
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}, reducedMotion: 'reduce'});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  /* Simulate a proxy that cannot carry EventSource at all. */
  await page.addInitScript(() => { window.EventSource = undefined; });
  await page.goto(base + '/dashboard');
  await page.locator('#guiToken').fill(token);
  await page.locator('#guiLogin button').click();
  await page.waitForFunction(() => document.getElementById('metaUplink').textContent === 'online');
  try {
    await page.waitForFunction(() => ['polling', 'offline'].includes(document.getElementById('msStream').dataset.state), null, {timeout: 15000});
    assert.equal(await page.locator('#msStream').getAttribute('data-state'), 'polling');
    assert.equal(await page.locator('#msStream [data-i18n]').getAttribute('data-i18n'), 'mission.stream.polling');
    /* the tiles still fill from the summary endpoint */
    await page.waitForFunction(() => /^\d+$/.test(document.getElementById('msTasksActive').textContent.trim()));
    assert.equal(await page.locator('#sbSse').innerText(), 'SSE · idle');
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('the page stays honest in Bangla, at 360 px and without a run recorded', async () => {
  const {page, context, errors} = await open(360, 740);
  try {
    await page.evaluate(() => window.nexusI18n.setLang('bn'));
    await page.waitForFunction(() => document.documentElement.lang === 'bn');
    assert.match(await page.locator('#msStream [data-i18n]').getAttribute('data-i18n'), /^mission\.stream\./);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0, 'no horizontal overflow');
    /* the health strip is a single readable column on a phone */
    const box = await page.locator('#msHealth').boundingBox();
    assert.ok(box.width <= 360, 'health strip ' + JSON.stringify(box));
    assert.equal(await page.locator('.mission-tile').count(), 6);
    /* quickly switching language must not leave an untranslated key */
    assert.deepEqual(await page.evaluate(() => window.nexusI18n.missing()), []);
    await page.evaluate(() => window.nexusI18n.setLang('en'));
    assert.match(await page.locator('#msDaemonAge').innerText(), /ago/);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});
