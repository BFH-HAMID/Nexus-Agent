/** Task 12 panel regressions. Real DOM/Chromium, in-page fake APIs only.
 * No Nexus server, native app, network, credentials or device effects. */
import {test, before, after} from 'node:test';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {chromium} from 'playwright-core';
import bundled from '@sparticuz/chromium';

let browser;
const asset = name => fileURLToPath(new URL(`../../nexus/panel/${name}`, import.meta.url));
before(async () => {
  browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || await bundled.executablePath(),
    args: bundled.args.filter(a => !['--single-process', '--disable-web-security'].includes(a)),
    headless: true,
  });
});
after(async () => { await browser?.close(); });

async function panel(name, width = 1400) {
  const context = await browser.newContext({viewport: {width, height: 1000}, reducedMotion: 'reduce'});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  // Fail closed if a regression tries to turn a fixture into a real API request.
  await context.route('**/*', route => route.abort());
  await page.setContent('<!doctype html><html lang="en"><head></head><body><main class="deck"></main></body></html>');
  for (const css of ['dashboard.css', 'shell.css', 'assistant.css']) await page.addStyleTag({path: asset(css)});
  await page.evaluate(() => {
    window.calls = [];
    window.shown = '';
    window.savedText = 'before';
    window.nexusI18n = {t: key => key, apply: () => {}, onChange: () => {}};
    window.nexusGui = {
      view: 'run', ready: Promise.resolve(),
      show(view) { window.shown = view; },
      async api(path, body) {
        window.calls.push({path, body});
        if (path === '/api/computer/action') return {data: {task_id: 'queued-fixture', queued: true}};
        if (path === '/api/computer/action/review') return {data: {
          id: 'one-use-fixture', confirmation: 'ALLOW COMPUTER fixture-123', preview: 'Exact work/a.txt review',
        }};
        if (path === '/api/computer/file/preview') return {data: {
          path: 'work/a.txt', text: window.savedText, size: window.savedText.length, revision: 'fixture', binary: false,
        }};
        if (path === '/api/computer/file/write') {
          window.savedText = body.text;
          return {data: {saved: body.path, bytes: body.text.length, snapshot: 'fixture-snapshot'}};
        }
        if (path === '/api/assistant/automation/backup') return {id: 'pending-backup', task_id: 'queued-backup', queued: true};
        if (path === '/api/assistant/automation/restore') return {task_id: 'queued-restore', queued: true};
        throw Error('No fixture for ' + path);
      },
    };
  });
  await page.addScriptTag({path: asset(name + '.js')});
  await page.locator('#view-' + name).evaluate(node => { node.hidden = false; });
  return {page, context, errors};
}

for (const width of [1400, 360]) test(`computer queue reports admission and opens approvals (${width}px)`, async () => {
  const {page, context, errors} = await panel('computer', width);
  try {
    await page.locator('#ccActionQueue').click();
    await page.waitForFunction(() => document.getElementById('ccActionStatus').textContent.includes('queued-fixture'));
    assert.match(await page.locator('#ccActionStatus').innerText(), /queued.*fresh action.*Approvals/);
    assert.equal(await page.locator('#ccActionQueue').isDisabled(), false);
    assert.deepEqual(await page.evaluate(() => window.calls.map(call => call.path)), ['/api/computer/action']);
    await page.locator('#ccActionApprovals').click();
    assert.equal(await page.evaluate(() => window.shown), 'approvals');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('legacy writes request one-use proof and execute only the payload that was previewed', async () => {
  const {page, context, errors} = await panel('computer');
  try {
    await page.evaluate(async () => {
      window.nexusComputer.tab('files');
      await window.nexusComputer.previewFile('work/a.txt');
    });
    await page.locator('#ccText').fill('exact reviewed body');
    await page.locator('#ccWrite').click();
    await page.waitForSelector('#ccConfirmRow:not([hidden])');
    await page.locator('#ccText').fill('later edit is not silently approved');
    await page.locator('#ccConfirm').fill('WRITE work/a.txt');
    await page.locator('#ccConfirmGo').click();
    assert.equal(await page.evaluate(() => window.calls.filter(c => c.path.endsWith('/file/write')).length), 0);
    assert.match(await page.locator('#ccFileStatus').innerText(), /wrongToken/);
    await page.locator('#ccConfirm').fill('ALLOW COMPUTER fixture-123');
    await page.locator('#ccConfirmGo').click();
    await page.waitForFunction(() => window.calls.some(c => c.path.endsWith('/file/write')));
    const write = await page.evaluate(() => window.calls.find(c => c.path.endsWith('/file/write')).body);
    assert.equal(write.text, 'exact reviewed body');
    assert.equal(write.approval_id, 'one-use-fixture');
    assert.equal(write.approval, 'ALLOW COMPUTER fixture-123');
    assert.equal(await page.evaluate(() => window.nexusComputer.state().pending), null);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('an expired or refused legacy action reports at its originating panel without scope errors', async () => {
  const {page, context, errors} = await panel('computer');
  try {
    await page.evaluate(() => {
      window.nexusComputer.tab('browser');
      window.nexusComputer.confirmAction('ALLOW COMPUTER expired', 'Exact request', async () => {
        throw Error('Fresh review expired; preview again');
      }, document.getElementById('ccBrowserStatus'));
    });
    await page.locator('#ccConfirm').fill('ALLOW COMPUTER expired');
    await page.locator('#ccConfirmGo').click();
    await page.waitForFunction(() => document.getElementById('ccBrowserStatus').textContent.includes('Fresh review expired'));
    assert.equal(await page.evaluate(() => window.nexusComputer.state().pending), null);
    assert.equal(await page.locator('#ccConfirmRow').isVisible(), false);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('assistant backup and restore are labelled as queued, with a direct approval link', async () => {
  const {page, context, errors} = await panel('assistant', 360);
  try {
    await page.locator('#asBackup').click();
    await page.locator('#asConfirmInput').fill('CREATE BACKUP');
    await page.locator('dialog.assistant-confirm button[value="confirm"]').click();
    await page.waitForFunction(() => document.getElementById('asBackupOut').textContent.includes('queued-backup'));
    assert.equal(await page.locator('#asBackupId').inputValue(), 'pending-backup');
    assert.match(await page.locator('#asBackupOut').innerText(), /queued for fresh approval, not a completed/);
    await page.locator('#asRestore').click();
    await page.locator('#asConfirmInput').fill('RESTORE BACKUP pending-backup');
    await page.locator('dialog.assistant-confirm button[value="confirm"]').click();
    await page.waitForFunction(() => document.getElementById('asBackupOut').textContent.includes('queued-restore'));
    await page.locator('#asAutomationApprovals').click();
    assert.equal(await page.evaluate(() => window.shown), 'approvals');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});


test('long exact previews remain readable without widening a phone viewport', async () => {
  const {page, context, errors} = await panel('computer', 360);
  try {
    await page.evaluate(() => window.nexusComputer.confirmAction(
      'ALLOW COMPUTER fixture-long', 'Exact input: ' + 'a'.repeat(4000), async () => {},
      document.getElementById('ccActionStatus')));
    assert.equal(await page.locator('#ccConfirmRow').isVisible(), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});
