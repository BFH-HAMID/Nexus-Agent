/** Prompt 12 acceptance: the World desk over the real server.
 *
 *  The desk's central promise is that *looking* costs nothing, so the suite
 *  counts every request the page makes to a public host while the desk loads
 *  and fails if there is even one. The only write is opened, checked and
 *  cancelled — this suite never asks a publisher for anything, and it restores
 *  nothing because it changes nothing.
 */
import {test, before, after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import bundled from '@sparticuz/chromium';

const base = process.env.NEXUS_TEST_URL || 'http://127.0.0.1:8787';
const token = process.env.NEXUS_GUI_TEST_TOKEN;
const PUBLIC = /^https?:\/\/(?!127\.0\.0\.1|localhost)/i;
let browser;
let context;
let page;
let errors = [];
let external = [];

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

async function signIn(target) {
  await target.goto(base + '/dashboard?map=2d');
  await target.locator('#guiToken').fill(token);
  await target.locator('#guiLogin button').click();
  await target.waitForFunction(() => document.getElementById('metaUplink').textContent === 'online', null, {timeout: 20000});
}

async function openWorld() {
  await page.locator('.sidebar-nav [data-nav-id="world"]').click();
  await page.waitForFunction(() => (window.nexusWorldDesk?.state().feeds ?? 0) > 0, null, {timeout: 30000});
}

before(async () => {
  assert.ok(token, 'Set NEXUS_GUI_TEST_TOKEN for the live dashboard');
  browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || await bundled.executablePath(),
    args: bundled.args.filter((a) => !['--single-process', '--disable-web-security'].includes(a)),
    headless: true,
  });
  context = await browser.newContext({viewport: {width: 1500, height: 1050}, reducedMotion: 'reduce'});
  page = await context.newPage();
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('request', (request) => { if (PUBLIC.test(request.url())) external.push(request.url()); });
  await signIn(page);
  await openWorld();
  // The globe itself may pull its own public feeds; the desk's numbers were
  // already painted before that, so start counting from here for the asserts.
});

after(async () => {
  await context?.close();
  await browser?.close();
});

test('the desk repeats the API exactly, and reading it contacts nobody', async () => {
  const overview = await api('/api/world/overview');
  const shown = await page.evaluate(() => window.nexusWorldDesk.state());
  assert.equal(shown.feeds, overview.totals.feeds);
  assert.equal(shown.fresh, overview.totals.fresh);
  assert.equal(shown.headlines, overview.totals.headlines);
  assert.equal(shown.keyPresent, overview.key_present);
  assert.equal(shown.enabled, overview.enabled);
  // Six KPIs, one row per configured feed — never a padded list.
  assert.equal(await page.locator('#wdKpis .security-count').count(), 6);
  assert.equal(await page.locator('#wdRows tr').count(), overview.sources.length);
  // A feed that was never fetched says so; it is not shown as "0".
  const states = await page.locator('#wdRows tr').evaluateAll((rows) => rows.map((row) => row.dataset.state));
  assert.deepEqual(states, overview.sources.map((row) => row.status));
  // The stamp names the moment the figures were read.
  assert.doesNotMatch(await page.locator('#wdStamp').textContent(), /^$/);
  const stale = await page.evaluate(() => document.getElementById('wdActionHost').hidden);
  assert.equal(stale, true, 'no confirmation sheet is open on arrival');
});

test('every source row carries its group, age, cadence and publisher terms', async () => {
  const overview = await api('/api/world/overview');
  const first = overview.sources[0];
  const row = page.locator('#wdRows tr').first();
  assert.match(await row.textContent(), new RegExp(first.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  assert.equal(await row.locator('a.worlddesk-terms').getAttribute('href'), first.terms);
  assert.equal(await row.locator('a.worlddesk-terms').getAttribute('rel'), 'noopener noreferrer');
  // The cadence column is the real TTL from widget.yaml, not a rounded guess.
  const cadence = await row.locator('td').nth(4).textContent();
  assert.equal(cadence.trim(), `${Math.round(first.ttl_s / 60)}m`);
  // Each cell is labelled, so the phone card layout is honest by construction.
  assert.equal(await row.locator('td[data-label]').count(), 6);
});

test('contacting sources needs the typed token, and cancelling writes nothing', async () => {
  const before = await api('/api/world/overview');
  await page.locator('#wdRefresh').click();
  await page.waitForFunction(() => !document.getElementById('wdActionHost').hidden, null, {timeout: 10000});
  assert.equal(await page.locator('#wdActionExpected').textContent(), 'REFRESH PUBLIC SOURCES');
  assert.equal(await page.locator('#wdActionGo').isDisabled(), true, 'the button is dead until the token matches');
  await page.locator('#wdActionToken').fill('REFRESH PUBLIC SOURCE');
  assert.equal(await page.locator('#wdActionGo').isDisabled(), true, 'a near-miss is still refused');
  await page.locator('#wdActionToken').fill('REFRESH PUBLIC SOURCES');
  assert.equal(await page.locator('#wdActionGo').isDisabled(), false);
  await page.locator('#wdActionCancel').click();
  assert.equal(await page.evaluate(() => document.getElementById('wdActionHost').hidden), true);
  const after = await api('/api/world/overview');
  assert.deepEqual(after.sources.map((row) => [row.cache_key, row.at]), before.sources.map((row) => [row.cache_key, row.at]),
    'cancelling fetched nothing and changed nothing');
  // The same refusal over HTTP: no token, no request to a publisher.
  const reply = await fetch(base + '/api/world/refresh', {
    method: 'POST', headers: headers(), body: JSON.stringify({groups: ['news']}),
  });
  assert.equal(reply.status, 400);
  assert.match((await reply.json()).error.message, /REFRESH PUBLIC SOURCES/);
});

test('weather is only offered when its credential exists, and says why', async () => {
  const overview = await api('/api/world/overview');
  await page.locator('#wdRefresh').click();
  await page.waitForFunction(() => !document.getElementById('wdActionHost').hidden, null, {timeout: 10000});
  const weather = page.locator('#wdGroup-weather');
  if (overview.key_present || overview.provider !== 'weatherapi') {
    assert.equal(await weather.isDisabled(), false);
  } else {
    assert.equal(await weather.isDisabled(), true, 'no key means no weather request');
    assert.equal(await weather.isChecked(), false);
    const hint = await page.locator('#wdWeatherHint').textContent();
    assert.match(hint, /WeatherAPI|configuration-required/i);
  }
  const facts = await page.locator('#wdWeatherFacts').textContent();
  assert.match(facts, /WeatherAPI\.com|Open-Meteo/);
  assert.match(facts, /every \d+/i);
  await page.locator('#wdActionCancel').click();
});

test('a phone-width Bengali desk keeps every cell labelled and never overflows', async () => {
  await page.evaluate(() => window.nexusI18n.setLang('bn'));
  await page.setViewportSize({width: 360, height: 780});
  await page.waitForFunction(() => document.documentElement.lang === 'bn', null, {timeout: 10000});
  await page.waitForTimeout(500);
  assert.match(await page.locator('#worldDesk h1').textContent(), /[\u0980-\u09FF]/);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert.ok(overflow <= 2, 'the desk must not scroll sideways at 360 px: ' + overflow);
  // Cards: each visible cell still carries its column name.
  const labels = await page.locator('#wdRows tr:first-child td[data-label]').evaluateAll((cells) =>
    cells.map((cell) => cell.dataset.label));
  assert.ok(labels.length >= 4 && labels.every((label) => label && label.length > 0), JSON.stringify(labels));
  await page.setViewportSize({width: 1500, height: 1050});
  await page.evaluate(() => window.nexusI18n.setLang('en'));
  await page.waitForFunction(() => document.documentElement.lang === 'en', null, {timeout: 10000});
});

test('the desk never sent a single request to a public host while it was open', async () => {
  // The globe caches its own feeds; whatever it fetched happened before the
  // desk was read. From that point the desk itself must be silent.
  external = [];
  await page.locator('.sidebar-nav [data-nav-id="mission"]').click();
  await page.locator('.sidebar-nav [data-nav-id="world"]').click();
  await page.waitForTimeout(2500);
  assert.deepEqual(external, [], 'opening the desk must not contact any publisher');
  assert.equal(errors.length, 0, errors.join('\n'));
});
