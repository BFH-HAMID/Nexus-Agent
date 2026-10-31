/** Prompt 8 acceptance: the Security Center over the real server.

 *  Nothing here is a mock. The audit rows are the server's own trail, the
 *  sessions are cookies it really issued, the policy edit changes the file the
 *  approval gate loads, and the vault write goes into the same store the CLI
 *  uses. The suite restores what it changes: the policy default and its tools,
 *  the stored key, and the second factor (enrolment is disabled again at the
 *  end of the test that enables it).
 */
import {test, before, after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import bundled from '@sparticuz/chromium';
import crypto from 'node:crypto';
import fs from 'node:fs';

const base = process.env.NEXUS_TEST_URL || 'http://127.0.0.1:8787';
const token = process.env.NEXUS_GUI_TEST_TOKEN;
const RUN = Date.now().toString(36);
const SECRET_MARK = `sk-nexus-${RUN}-` + 'x'.repeat(24);
let browser;
let context;
let page;
let errors = [];
let secondContext = null;

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

function base32(secret) {
  const map = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  let bits = '';
  for (const ch of String(secret).toUpperCase().replace(/=+$/, '')) bits += map.indexOf(ch).toString(2).padStart(5, '0');
  const bytes = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(bytes);
}

function totp(secret, step) {
  const buffer = Buffer.alloc(8);
  buffer.writeBigUInt64BE(BigInt(step));
  const digest = crypto.createHmac('sha1', base32(secret)).update(buffer).digest();
  const offset = digest[digest.length - 1] & 0x0f;
  return String((digest.readUInt32BE(offset) & 0x7fffffff) % 1e6).padStart(6, '0');
}

const step = () => Math.floor(Date.now() / 30000);

async function signIn(target) {
  await target.goto(base + '/dashboard');
  await target.locator('#guiToken').fill(token);
  await target.locator('#guiLogin button').click();
  await target.waitForFunction(() => document.getElementById('metaUplink').textContent === 'online', {timeout: 20000});
}

async function openSecurity(tab = 'overview') {
  await page.locator('.sidebar-nav [data-nav-id="security"]').click();
  await page.locator(`[data-sec-tab="${tab}"]`).click();
  await page.waitForFunction((name) => !document.querySelector(`[data-sec-panel="${name}"]`)?.hidden, tab, {timeout: 15000});
}

async function confirmWord(scope, word) {
  await page.locator(`${scope} #secActionToken`).waitFor({timeout: 10000});
  await page.locator(`${scope} #secActionToken`).fill(word);
  assert.equal(await page.locator(`${scope} #secActionGo`).isDisabled(), false, 'the confirm button stays disabled');
  await page.locator(`${scope} #secActionGo`).click();
}

before(async () => {
  assert.ok(token, 'Set NEXUS_GUI_TEST_TOKEN for the live dashboard');
  browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || await bundled.executablePath(),
    args: bundled.args.filter((a) => !['--single-process', '--disable-web-security'].includes(a)),
    headless: true,
  });
  context = await browser.newContext({viewport: {width: 1600, height: 1000}, reducedMotion: 'reduce'});
  page = await context.newPage();
  page.on('pageerror', (error) => errors.push(error.message));
  await signIn(page);
  await openSecurity('overview');
  await page.waitForFunction(() => document.querySelectorAll('#secKpis .security-count').length === 6, {timeout: 20000});
});

after(async () => {
  // The test that enrols two-factor disables it again; this is a second net.
  if (await api('/api/security/two-factor').then((body) => body.enrolled).catch(() => false)) {
    errors.push('two-factor was still enrolled after the suite');
  }
  await secondContext?.close();
  await context?.close();
  await browser?.close();
});

test('the sidebar opens Security and every counter comes from the API', async () => {
  const overview = await api('/api/security/overview');
  const cards = page.locator('#secKpis .security-count');
  assert.equal(await cards.count(), 6);
  const chainValue = (await cards.nth(0).locator('b').textContent()).trim();
  assert.equal(chainValue, overview.audit.chain === 'valid' ? 'Intact' : 'Broken');
  const lines = (await cards.nth(1).locator('b').textContent()).trim();
  // The page's own reads are sampled into the same trail, so the API can be a
  // line or two ahead of the paint; the chip and the card must still agree.
  assert.ok(Math.abs(Number(lines) - Number(overview.audit.counted)) <= 2,
    `painted ${lines}, API counted ${overview.audit.counted}`);
  assert.match(await page.locator('#secChip').textContent(), new RegExp(String(lines)));
  assert.equal(errors.length, 0, errors.join('\n'));
});

test('the audit tab shows real rows, verifies the chain and filters by kind', async () => {
  await openSecurity('audit');
  await page.waitForFunction(() => document.querySelectorAll('#secAuditRows tr').length > 0, {timeout: 15000});
  const firstAction = (await page.locator('#secAuditRows tr').first().locator('td').nth(2).textContent()).trim();
  assert.ok(firstAction.length > 3, 'a real action code is shown');
  await page.locator('#secVerify').click();
  await page.waitForFunction(() => {
    const node = document.querySelector('#secChain');
    return node && /checked|Intact|Broken/.test(node.textContent);
  }, {timeout: 15000});
  assert.match(await page.locator('#secChain').textContent(), /Intact|Broken/);
  await page.locator('#secAuditFilter').selectOption('security.');
  await page.waitForTimeout(700);
  const actions = await page.locator('#secAuditRows tr td:nth-child(3)').allTextContents();
  assert.ok(actions.length > 0, 'the filter keeps matching rows');
  assert.ok(actions.every((action) => action.trim().startsWith('security.')), actions.join(','));
  await page.locator('#secAuditFilter').selectOption('');
  await page.waitForTimeout(500);
});

test('a policy save needs the exact token and changes the file the gate loads', async () => {
  const before = await api('/api/security/policy');
  await openSecurity('sandbox');
  const wanted = before.default === 'READ_ONLY' ? 'ASK_EVERY_TIME' : 'READ_ONLY';
  await page.locator(`input[name="secDefaultMode"][value="${wanted}"]`).check();
  await page.locator('#secPolicySave').click();
  await page.locator('#secActionToken').fill('save the policy');
  assert.equal(await page.locator('#secActionGo').isDisabled(), true);
  await page.locator('#secActionToken').fill('SAVE CAPABILITY POLICY');
  await page.locator('#secActionGo').click();
  await page.waitForFunction((mode) => window.nexusSecurity?.state().policy === mode, wanted, {timeout: 15000});
  assert.equal((await api('/api/security/policy')).default, wanted);
  // Put the operator's value back through the same form.
  await page.locator(`input[name="secDefaultMode"][value="${before.default}"]`).check();
  await page.locator('#secPolicySave').click();
  await confirmWord('#secActionHost', 'SAVE CAPABILITY POLICY');
  await page.waitForFunction((mode) => window.nexusSecurity?.state().policy === mode, before.default, {timeout: 15000});
  assert.equal((await api('/api/security/policy')).default, before.default);
});

test('revoking another browser kills its cookie while this one keeps working', async () => {
  secondContext = await browser.newContext({viewport: {width: 1200, height: 900}, reducedMotion: 'reduce'});
  const other = await secondContext.newPage();
  await signIn(other);
  const otherId = await other.evaluate(async () => (await (await fetch('/api/security/sessions')).json()).data.current);
  assert.match(otherId, /^[0-9a-f]{12}$/);
  await openSecurity('access');
  const row = page.locator('#secSessionRows tr').filter({hasText: otherId});
  await row.waitFor({timeout: 15000});
  await row.locator('button').click();
  await confirmWord('#secActionHost', `REVOKE SESSION ${otherId}`);
  await page.waitForFunction((id) => {
    const table = document.getElementById('secSessionRows');
    return table && table.textContent.includes(id);
  }, otherId, {timeout: 15000});
  const status = await other.evaluate(async () => (await fetch('/api/security/sessions')).status);
  assert.equal(status, 401, 'the revoked cookie is refused');
  const mine = await page.evaluate(async () => (await fetch('/api/security/sessions')).status);
  assert.equal(mine, 200, 'the current browser is untouched');
  await secondContext.close();
  secondContext = null;
});

test('two-factor shows the secret once, needs a code plus its token, and turns off cleanly', async () => {
  await openSecurity('access');
  await page.locator('#secTotpBody button').filter({hasText: 'Start enrolment'}).click();
  await page.locator('.security-secret').waitFor({timeout: 10000});
  const secret = (await page.locator('.security-secret').textContent()).trim();
  assert.match(secret, /^[A-Z2-7]{20,}$/);
  await page.locator('#secEnableCode').fill(totp(secret, step()));
  await page.locator('#secTotpBody button').filter({hasText: 'Confirm code'}).click();
  await page.locator('#secActionToken').fill('ENABLE 2FA');
  // The six-digit code was already given to the enrolment form, so this sheet
  // asks only for its token; a second code prompt here would be theatre.
  assert.equal(await page.locator('#secActionCode').count(), 0);
  await page.locator('#secActionGo').click();
  await page.locator('.security-recovery li').first().waitFor({timeout: 15000});
  assert.equal(await page.locator('.security-recovery li').count(), 8);
  const body = await page.locator('.security-recovery').textContent();
  assert.ok(!body.includes(secret), 'the enrolment secret is not repeated next to the recovery codes');
  assert.equal((await api('/api/security/two-factor')).enrolled, true);
  await page.locator('button').filter({hasText: 'I have stored them'}).click();
  await page.locator('button').filter({hasText: 'Turn off two-factor'}).click();
  await page.locator('#secActionToken').fill('DISABLE 2FA');
  assert.equal(await page.locator('#secActionCode').count(), 1, 'an enrolled factor asks for a live code');
  assert.equal(await page.locator('#secActionGo').isDisabled(), true, 'token alone is not enough once enrolled');
  await page.locator('#secActionCode').fill(totp(secret, step() + 1));
  assert.equal(await page.locator('#secActionGo').isDisabled(), false);
  await page.locator('#secActionGo').click();
  await page.waitForFunction(() => window.nexusSecurity?.state().enrolled === false, {timeout: 15000});
  assert.equal((await api('/api/security/two-factor')).enrolled, false);
});

test('the vault writes a key without ever echoing it, then removes it', async () => {
  await openSecurity('vault');
  await page.locator('#secSecretName').selectOption('deepseek');
  await page.locator('#secSecretValue').fill(SECRET_MARK);
  await page.locator('#secSecretSave').click();
  await confirmWord('#secActionHost', 'SAVE SECRET deepseek');
  await page.waitForFunction(() => document.getElementById('secSecretValue').value === '', {timeout: 15000});
  const vault = await api('/api/security/vault');
  const row = vault.items.find((item) => item.name === 'deepseek');
  assert.equal(row.set, true);
  assert.match(row.fingerprint, /^[0-9a-f]{10}$/);
  assert.ok(!JSON.stringify(vault).includes(SECRET_MARK), 'the value is not returned by the API');
  assert.equal(await page.locator('#secSecretValue').inputValue(), '', 'the field is cleared after saving');
  assert.ok(!(await page.locator('#view-security').innerText()).includes(SECRET_MARK), 'the value never reaches the page');
  await page.locator('#secVaultRows button').filter({hasText: 'Remove'}).first().click();
  await confirmWord('#secActionHost', 'DELETE SECRET deepseek');
  await page.waitForTimeout(600);
  assert.equal((await api('/api/security/vault')).items.find((item) => item.name === 'deepseek').set, false);
});

test('a snapshot can be taken, downloaded and put back', async () => {
  await openSecurity('audit');
  const before = (await api('/api/security/snapshots')).snapshots.length;
  await page.locator('#secSnapshotTake').click();
  await confirmWord('#secActionHost', 'TAKE SECURITY SNAPSHOT');
  await page.waitForFunction((count) => document.querySelectorAll('#secSnapshotRows tr').length > count,
    before, {timeout: 15000});
  const list = await api('/api/security/snapshots');
  const name = list.snapshots[0].name;
  assert.match(name, /^[0-9]{8}T[0-9]{6}Z(-[0-9]+)?$/);
  const row = page.locator('#secSnapshotRows tr').filter({hasText: name});
  await row.waitFor({timeout: 10000});

  const [download] = await Promise.all([
    page.waitForEvent('download', {timeout: 15000}),
    row.locator('button').filter({hasText: 'Download'}).click(),
  ]);
  assert.equal(download.suggestedFilename(), `${name}.json`);
  const saved = JSON.parse(fs.readFileSync(await download.path(), 'utf8'));
  assert.equal(saved.kind, 'nexus-security-snapshot');
  assert.equal(saved.audit.checked, list.snapshots[0].checked, 'the copy states how many lines it holds');
  assert.ok(!JSON.stringify(saved).includes('sk-'), 'no credential text is inside the copy');

  await row.locator('button').filter({hasText: 'Restore'}).click();
  await confirmWord('#secActionHost', `RESTORE SECURITY SNAPSHOT ${name}`);
  await page.waitForTimeout(1200);
  const after = await api('/api/security/audit?limit=1');
  assert.equal(after.chain.ok, true, 'the restored trail verifies');
  const check = await api('/api/security/snapshots');
  assert.ok(check.snapshots.some((entry) => entry.name === name));
  assert.equal(errors.length, 0, errors.join('\n'));
});

test('Bangla and a 360 px phone keep the page readable and usable', async () => {
  await openSecurity('overview');
  await page.evaluate(() => window.nexusI18n.setLang('bn'));
  await page.waitForTimeout(400);
  assert.match(await page.locator('#view-security h1').textContent(), /[\u0980-\u09FF]/);
  const text = await page.locator('#view-security').innerText();
  assert.ok(!/security\.[a-zA-Z]/.test(text), 'no untranslated key is printed');
  await page.setViewportSize({width: 360, height: 780});
  await page.waitForTimeout(500);
  const overflow = await page.evaluate(() => {
    const root = document.getElementById('view-security');
    return root.scrollWidth - root.clientWidth;
  });
  assert.ok(overflow <= 2, `the page overflows by ${overflow}px`);
  await page.locator('[data-sec-tab="audit"]').click();
  await page.waitForFunction(() => !document.getElementById('secPanelAudit').hidden, {timeout: 10000});
  await page.evaluate(() => window.nexusI18n.setLang('en'));
  await page.setViewportSize({width: 1600, height: 1000});
  await page.waitForTimeout(400);
  assert.equal(errors.length, 0, errors.join('\n'));
});
