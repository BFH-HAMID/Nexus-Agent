/** Prompt 11 acceptance: alerts, delivery, Telegram, voice and mobile-lite.
 *
 *  Everything reads the real server: the notices are the ones the Jarvis inbox
 *  holds, delivery is the policy that server actually obeys, and the Telegram
 *  card reports the prerequisites this machine really has. The suite writes two
 *  display preferences and puts both back before it finishes.
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

async function waitForApi(probe, ms = 20000) {
  const started = Date.now();
  let last = 'nothing';
  for (;;) {
    const value = await probe().catch((error) => { last = 'refused: ' + error.message; return null; });
    if (value) return value;
    if (Date.now() - started > ms) throw new Error('the API never reached the expected state (' + last + ')');
    await new Promise((done) => setTimeout(done, 900));
  }
}

async function signIn(target) {
  await target.goto(base + '/dashboard');
  await target.locator('#guiToken').fill(token);
  await target.locator('#guiLogin button').click();
  await target.waitForFunction(() => document.getElementById('metaUplink').textContent === 'online', {timeout: 20000});
}

async function openChannels(tab = 'inbox') {
  await page.locator('.sidebar-nav [data-nav-id="channels"]').click();
  await page.locator(`[data-ch-tab="${tab}"]`).click();
  await page.waitForFunction((name) => !document.querySelector(`[data-ch-panel="${name}"]`)?.hidden, tab, {timeout: 15000});
}

/* The sheet closes on success and keeps the server's refusal on screen; a
   silent timeout would hide which of the two happened. */
async function settle(ms = 15000) {
  const started = Date.now();
  for (;;) {
    if (await page.evaluate(() => document.getElementById('chActionHost').hidden)) return true;
    if (Date.now() - started > ms) {
      const message = await page.locator('#chActionError').textContent().catch(() => '');
      throw new Error('the confirm sheet stayed open: ' + (message || '(no message)'));
    }
    await page.waitForTimeout(200);
  }
}

async function confirmWord(word) {
  await page.locator('#chActionHost #chActionToken').waitFor({timeout: 10000});
  assert.equal(await page.locator('#chActionExpected').textContent(), word);
  assert.equal(await page.locator('#chActionGo').isDisabled(), true, 'the button is dead until the token matches');
  await page.locator('#chActionHost #chActionToken').fill(word);
  assert.equal(await page.locator('#chActionHost #chActionGo').isDisabled(), false);
  await page.locator('#chActionHost #chActionGo').click();
}

before(async () => {
  assert.ok(token, 'Set NEXUS_GUI_TEST_TOKEN for the live dashboard');
  original = await api('/api/channels/policy');
  browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || await bundled.executablePath(),
    args: bundled.args.filter((a) => !['--single-process', '--disable-web-security'].includes(a)),
    headless: true,
  });
  context = await browser.newContext({viewport: {width: 1600, height: 1000}, reducedMotion: 'reduce'});
  page = await context.newPage();
  page.on('pageerror', (error) => errors.push(error.message));
  await signIn(page);
  await openChannels('inbox');
  await page.waitForFunction(() => document.querySelectorAll('#chKpis .security-count').length === 6, null, {timeout: 20000});
});

after(async () => {
  try {
    if (original) {
      const view = await api('/api/channels/policy');
      await api('/api/channels/policy/save', {
        revision: view.revision, voice: original.policy.voice, mobile: original.policy.mobile,
      });
      await page.evaluate((prefs) => window.nexusMobile?.apply(prefs), original.policy.mobile).catch(() => {});
    }
  } catch (error) {
    errors.push('restore failed: ' + error.message);
  }
  await context?.close();
  await browser?.close();
});

test('the page opens on the real inbox with its provenance on screen', async () => {
  const overview = await api('/api/channels/overview');
  const state = await page.evaluate(() => window.nexusChannels.state());
  assert.equal(state.transport, overview.delivery.policy.transport);
  assert.equal(state.unread, overview.inbox.digest.unread);
  assert.equal(state.ready, overview.telegram.ready);
  assert.equal(await page.locator('#view-channels [data-ch-tab]').count(), 5);
  // The chip repeats the real transport and poller phase, not a friendly summary.
  const chip = await page.locator('#chChip').textContent();
  assert.match(chip, /delivery|ডেলিভারি/i);
  assert.doesNotMatch(await page.locator('#chUpdated').textContent(), /^$/);
  // The notice list is either real rows or an explicit empty state — never filler.
  const rows = await page.locator('#chRows .channels-row').count();
  const empty = await page.locator('#chRows .security-empty').count();
  assert.equal(rows + empty, 1, 'the list shows either notices or one honest empty line');
  assert.equal(rows, overview.inbox.items.length);
  assert.equal(errors.length, 0, errors.join('\n'));
});

test('each tab paints its own screen and nothing throws', async () => {
  for (const tab of ['delivery', 'telegram', 'voice', 'mobile', 'inbox']) {
    await page.locator(`[data-ch-tab="${tab}"]`).click();
    await page.waitForFunction((name) => !document.querySelector(`[data-ch-panel="${name}"]`)?.hidden, tab);
    assert.equal(await page.locator(`[data-ch-panel="${tab}"]`).isVisible(), true);
    if (tab === 'telegram') {
      await page.waitForFunction(() => document.querySelectorAll('#chTelegramChecks .channels-check').length === 4,
        null, {timeout: 15000});
      const checks = await page.locator('#chTelegramChecks .channels-check').count();
      assert.equal(checks, 4, 'every prerequisite is listed, met or not');
      const ready = await api('/api/channels/telegram');
      assert.equal(await page.locator('#chTelegramStart').isDisabled(), !ready.ready || Boolean(ready.runtime.alive));
    }
    if (tab === 'delivery') {
      const delivery = await api('/api/channels/delivery');
      await page.waitForFunction((value) => document.getElementById('chTransport').value === value,
        delivery.policy.transport, {timeout: 15000});
      // A missing OS notifier is named, not implied.
      if (!delivery.native.present) {
        const notes = await page.locator('#chDeliveryNotes').textContent();
        assert.ok(notes.includes(delivery.native.program), notes);
      }
    }
    if (tab === 'voice') {
      assert.ok(await page.evaluate(() => typeof window.nexusChannels.speak === 'function'));
      // The page explains its own privacy: speech out, never recording in.
      assert.ok(await page.locator('#chVoiceNotes li').count() >= 2);
      const notes = await page.locator('#chVoiceNotes').textContent();
      assert.match(notes, /record|upload|microphone|রেকর্ড|মাইক্রোফোন/i);
    }
    if (tab === 'mobile') {
      assert.equal(await page.locator('#chMobileLite').isChecked(), original.policy.mobile.lite);
    }
  }
  assert.equal(errors.length, 0, errors.join('\n'));
});

test('delivery only changes with the typed token, and the API agrees', async () => {
  await openChannels('delivery');
  const before = await api('/api/channels/delivery');
  const quiet = !before.policy.quiet;
  await page.waitForFunction((want) => document.getElementById('chQuiet').checked === want,
    before.policy.quiet, {timeout: 15000});
  await page.locator('#chQuiet').setChecked(quiet);
  await page.locator('#chSaveDelivery').click();
  await page.locator('#chActionHost #chActionToken').waitFor({timeout: 10000});
  await page.locator('#chActionCancel').click();
  assert.equal((await api('/api/channels/delivery')).policy.quiet, before.policy.quiet, 'cancelling saves nothing');

  await page.locator('#chSaveDelivery').click();
  await confirmWord('SAVE ALERT POLICY');
  assert.equal(await settle(), true, 'the sheet closes only after the server accepted the save');
  await waitForApi(async () => {
    const now = await api('/api/channels/delivery');
    if (now.policy.quiet !== quiet) throw new Error('quiet is ' + now.policy.quiet + ' (want ' + quiet + ')');
    return now;
  });
  // Put it back through the same sheet, so the run leaves the policy it found.
  await page.waitForFunction((want) => document.getElementById('chQuiet').checked === want, quiet, {timeout: 15000});
  await page.locator('#chQuiet').setChecked(before.policy.quiet);
  await page.locator('#chSaveDelivery').click();
  await confirmWord('SAVE ALERT POLICY');
  assert.equal(await settle(), true, 'the restore closes only after the server accepted it');
  await waitForApi(async () => {
    const now = await api('/api/channels/delivery');
    if (now.policy.quiet !== before.policy.quiet) {
      throw new Error('quiet is ' + now.policy.quiet + ' (want ' + before.policy.quiet + ')');
    }
    return now;
  });
  assert.equal(errors.length, 0, errors.join('\n'));
});

test('mobile-lite is a display preference that only rearranges the same data', async () => {
  await openChannels('mobile');
  await page.locator('#chMobileLite').setChecked(true);
  await page.locator('#chMobileCards').setChecked(true);
  await page.locator('#chSaveMobile').click();
  await page.waitForFunction(() => document.body.classList.contains('mobile-lite'), null, {timeout: 15000});
  assert.equal(await page.evaluate(() => window.nexusMobile.active()), true);
  const saved = await waitForApi(async () => {
    const view = await api('/api/channels/policy');
    return view.policy.mobile.lite ? view : null;
  });
  assert.equal(saved.policy.mobile.lite, true);
  // One column: the page must not scroll sideways on a phone.
  await page.setViewportSize({width: 360, height: 740});
  await page.waitForTimeout(400);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert.ok(overflow <= 2, 'mobile-lite must not overflow a 360 px screen: ' + overflow);
  // Tables become cards with their column names attached (checked on the cost page,
  // which is a different view and still the same, unchanged data).
  await page.setViewportSize({width: 1600, height: 1000});
  await page.locator('.sidebar-nav [data-nav-id="cost"]').click();
  await page.locator('[data-cost-tab="breakdown"]').click();
  await page.waitForFunction(() => document.querySelectorAll('#view-cost td[data-label]').length > 0, null, {timeout: 20000});
  const label = await page.locator('#view-cost td[data-label]').first().getAttribute('data-label');
  assert.ok(label && label.length > 0, 'each card cell carries its column name');
  // Back to the full layout.
  await openChannels('mobile');
  await page.locator('#chMobileLite').setChecked(false);
  await page.locator('#chSaveMobile').click();
  await page.waitForFunction(() => !document.body.classList.contains('mobile-lite'), null, {timeout: 15000});
  assert.equal(errors.length, 0, errors.join('\n'));
});

test('a phone-width Bengali page keeps every tab reachable', async () => {
  await page.evaluate(() => window.nexusI18n.setLang('bn'));
  await openChannels('inbox');
  await page.setViewportSize({width: 360, height: 740});
  await page.waitForFunction(() => /[\u0980-\u09FF]/.test(document.querySelector('#view-channels h1').textContent),
    null, {timeout: 10000});
  for (const tab of ['inbox', 'delivery', 'telegram', 'voice', 'mobile']) {
    assert.equal(await page.locator(`[data-ch-tab="${tab}"]`).isVisible(), true, tab);
  }
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert.ok(overflow <= 2, 'Bengali at 360 px must not scroll sideways: ' + overflow);
  // Keyboard: tabs are real buttons, so Enter selects the next one.
  await page.locator('[data-ch-tab="voice"]').focus();
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => !document.getElementById('chPanelVoice').hidden, null, {timeout: 10000});
  await page.setViewportSize({width: 1600, height: 1000});
  await page.evaluate(() => window.nexusI18n.setLang('en'));
  assert.equal(errors.length, 0, errors.join('\n'));
});
