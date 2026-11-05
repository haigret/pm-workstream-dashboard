/* Browser tests use a temporary SQLite database, never the working dashboard's data. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');
const ROOT = path.resolve(__dirname, '..');
const qa = path.join(ROOT, 'qa');
fs.mkdirSync(qa, {recursive: true});
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'pm-tracker-qa-'));
const bundled = path.join(os.homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3');
const python = process.env.PYTHON || (fs.existsSync(path.join(ROOT, '.venv/bin/python')) ? path.join(ROOT, '.venv/bin/python') : fs.existsSync(bundled) ? bundled : 'python3');
const server = spawn(python, ['-u', 'app.py', '--port', '0', '--db', path.join(temporary, 'qa.sqlite3')], {cwd: ROOT});
let serverLog = '';
server.stderr.on('data', data => { serverLog += data; });
const ready = new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error('Server startup timed out: ' + serverLog)), 15000);
  server.stdout.on('data', data => {
    const match = data.toString().match(/http:\/\/127\.0\.0\.1:\d+/);
    if (match) { clearTimeout(timer); resolve(match[0]); }
  });
  server.on('exit', code => { clearTimeout(timer); reject(new Error(`Server exited ${code}: ${serverLog}`)); });
});

(async () => {
  let browser;
  try {
    const url = await ready;
    browser = await chromium.launch({channel: 'chrome', headless: true});
    const context = await browser.newContext({viewport: {width: 1512, height: 1080}, deviceScaleFactor: 1});
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', dialog => dialog.accept());
    const data = async () => (await context.request.get(url + '/api/data')).json();
    const assertNoOverflow = async p => assert.equal(await p.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    const waitLoaded = async p => p.waitForFunction(() => document.querySelector('#connection').classList.contains('live'));

    await page.goto(url);
    await waitLoaded(page);
    assert.equal(await page.locator('#metricTotal').innerText(), '10');
    assert.equal(await page.locator('#metricProgress').innerText(), '06');
    assert.equal(await page.locator('#metricDelivered').innerText(), '00');
    assert.equal(await page.locator('#metricAttention').innerText(), '00');
    assert.equal(await page.locator('.workstream-row').count(), 5);
    assert.equal(await page.locator('.status-row').count(), 6);
    assert.equal(await page.locator('#recordRows tr').count(), 5);
    assert.doesNotMatch(await page.locator('body').innerText(), /LVMH|Maison|Market/i);
    assert.equal(await page.locator('img').count(), 2);
    assert.equal(await page.locator('img').evaluateAll(images => images.every(image => image.complete && image.naturalWidth > 0)), true);
    await assertNoOverflow(page);
    await page.screenshot({path: path.join(qa, 'overview-light.png'), fullPage: true});

    await page.locator('.workstream-row[data-stream="GR / billing & collection"]').click();
    assert.equal(await page.locator('#recordRows tr').count(), 2);
    assert.equal(await page.locator('#workstreamFilter').inputValue(), 'GR / billing & collection');
    await page.locator('#searchInput').fill('Crystal');
    assert.equal(await page.locator('#recordRows tr').count(), 1);
    await page.locator('#clearFilters').click();
    assert.equal(await page.locator('#recordRows tr').count(), 10);
    await page.locator('#sortDate').click();
    assert.equal(await page.locator('#recordRows tr .date-value').first().innerText(), '2026-11-30');
    await page.locator('#sortDate').click();
    assert.equal(await page.locator('#recordRows tr .date-value').first().innerText(), '2027-03-31');
    await page.locator('#clearFilters').click();
    await page.locator('[data-metric="attention"]').click();
    assert.match(await page.locator('#recordRows').innerText(), /No matching/);
    await page.locator('#clearFilters').click();
    await page.locator('#statusFilter').selectOption('In Progress');
    assert.equal(await page.locator('#recordRows tr').count(), 6);
    assert.equal(await page.locator('#metricTotal').innerText(), '10');
    await page.locator('#clearFilters').click();
    await page.screenshot({path: path.join(qa, 'tracker-light.png'), fullPage: true});

    // Staged edit changes the dashboard but not the database; reload restores the draft.
    const initial = await data();
    await page.locator('.edit-record').first().click();
    assert.equal(await page.locator('#editor').evaluate(dialog => dialog.open), true);
    await page.locator('[name="owner"]').fill('QA Owner');
    await page.locator('[name="status"]').selectOption('Delivered');
    await page.locator('[name="progress"]').fill('QA saved update\n下一步：验证');
    await page.screenshot({path: path.join(qa, 'editor-desktop.png')});
    await page.getByRole('button', {name: 'Apply draft', exact: true}).click();
    assert.equal(await page.locator('#metricDelivered').innerText(), '01');
    assert.equal((await data()).records[0].owner, initial.records[0].owner);
    assert.equal(await page.locator('#draftCount').innerText(), '1 unsaved change');
    await page.reload();
    await waitLoaded(page);
    assert.match(await page.locator('#restoredBanner').innerText(), /Restored 1 unsaved change/);
    assert.equal(await page.locator('#metricDelivered').innerText(), '01');
    const savedResponse = page.waitForResponse(response => response.url().endsWith('/api/save'));
    await page.locator('#saveButton').click();
    assert.equal((await savedResponse).status(), 200);
    await page.waitForFunction(() => document.querySelector('#draftDock').hidden);
    assert.equal((await data()).records[0].owner, 'QA Owner');
    assert.equal((await data()).records[0].progress, 'QA saved update\n下一步：验证');
    await page.reload();
    await waitLoaded(page);
    assert.equal(await page.locator('#metricDelivered').innerText(), '01');
    assert.equal(await page.locator('#saveButton').isDisabled(), true);

    // All original fields, Unicode, dates and literal HTML survive new-row persistence.
    await page.locator('#addButton').click();
    await page.locator('[name="intervention"]').fill('QA 新增交付 <img src=x onerror=alert(1)>');
    await page.locator('[name="workstream"]').fill('Enabler');
    await page.locator('[name="owner"]').fill('新增负责人');
    await page.locator('[name="edge_owner"]').fill('EDGE 对接人');
    await page.locator('[name="status"]').selectOption('Blocked');
    await page.locator('[name="target_date"]').fill('2026-01-01');
    await page.locator('[name="progress"]').fill('已完成范围确认；\n下一步：测试本地保存。');
    await page.locator('[name="blocker"]').fill('需要接口权限 <script>throw new Error("XSS")</script>');
    await page.getByRole('button', {name: 'Apply draft', exact: true}).click();
    assert.equal(await page.locator('#metricTotal').innerText(), '11');
    assert.equal(await page.locator('#metricAttention').innerText(), '01'); // Blocked + overdue counts once.
    assert.equal(await page.locator('#recordRows tr img, #recordRows tr script').count(), 0);
    assert.equal((await data()).records.length, 10);
    await page.locator('#saveButton').click();
    await page.waitForFunction(() => document.querySelector('#draftDock').hidden);
    const added = (await data()).records.at(-1);
    assert.equal(added.target_date, '2026-01-01');
    assert.equal(added.owner, '新增负责人');
    assert.match(added.blocker, /<script>/);
    assert.equal((await data()).records.length, 11);
    await page.reload();
    await waitLoaded(page);
    assert.equal(await page.locator('#metricTotal').innerText(), '11');
    assert.equal(await page.locator('#metricAttention').innerText(), '01');

    // A failed save retains in-memory and reloadable drafts.
    await page.locator('#tabTracker').click();
    await page.locator('.edit-record').first().click();
    await page.locator('[name="progress"]').fill('Must survive a failed save');
    await page.getByRole('button', {name: 'Apply draft', exact: true}).click();
    await page.route('**/api/save', route => route.fulfill({status: 500, contentType: 'application/json', body: JSON.stringify({error: 'Simulated save failure. No changes were committed.'})}));
    await page.locator('#saveButton').click();
    await page.waitForFunction(() => !document.querySelector('#errorBanner').hidden);
    assert.equal(await page.locator('#draftDock').isVisible(), true);
    assert.equal((await data()).records[0].progress, 'QA saved update\n下一步：验证');
    await page.unroute('**/api/save');
    await page.reload();
    await waitLoaded(page);
    assert.equal(await page.locator('#draftCount').innerText(), '1 unsaved change');
    await page.locator('#saveButton').click();
    await page.waitForFunction(() => document.querySelector('#draftDock').hidden);
    assert.equal((await data()).records[0].progress, 'Must survive a failed save');

    // A stale second window cannot overwrite the first window's newer revision.
    const secondContext = await browser.newContext({viewport: {width: 1280, height: 900}});
    const second = await secondContext.newPage();
    second.on('pageerror', error => errors.push(error.message));
    second.on('dialog', dialog => dialog.accept());
    await second.goto(url);
    await waitLoaded(second);
    for (const [p, owner] of [[page, 'Winning window'], [second, 'Stale window']]) {
      await p.locator('.edit-record').first().click();
      await p.locator('[name="owner"]').fill(owner);
      await p.getByRole('button', {name: 'Apply draft', exact: true}).click();
    }
    await page.locator('#saveButton').click();
    await page.waitForFunction(() => document.querySelector('#draftDock').hidden);
    const conflictResponse = second.waitForResponse(response => response.url().endsWith('/api/save'));
    await second.locator('#saveButton').click();
    assert.equal((await conflictResponse).status(), 409);
    assert.match(await second.locator('#errorText').innerText(), /Another window/);
    assert.equal(await second.locator('#draftDock').isVisible(), true);
    assert.equal((await data()).records[0].owner, 'Winning window');
    const download = second.waitForEvent('download');
    await second.locator('#exportDraft').click();
    assert.match((await download).suggestedFilename(), /PM-McKinsey-draft/);
    await second.locator('#discardButton').click();
    await secondContext.close();

    // Verify theme and responsive layouts after exercising saved data.
    await page.reload();
    await waitLoaded(page);
    await page.locator('#themeToggle').click();
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
    await page.screenshot({path: path.join(qa, 'overview-dark.png'), fullPage: true});
    await page.locator('#themeToggle').click();
    await page.setViewportSize({width: 390, height: 844});
    await page.evaluate(() => scrollTo(0, 0));
    await assertNoOverflow(page);
    await page.screenshot({path: path.join(qa, 'mobile-light.png'), fullPage: true});
    await page.locator('.workstream-row').first().click();
    await assertNoOverflow(page);
    await page.locator('.edit-record').first().click();
    await assertNoOverflow(page);
    assert.equal(await page.locator('#editor').evaluate(dialog => dialog.scrollWidth <= innerWidth), true);
    await page.screenshot({path: path.join(qa, 'editor-mobile.png')});
    await page.locator('#editorClose').click();
    await page.setViewportSize({width: 1512, height: 1080});
    assert.deepEqual(errors, []);
    console.log('PASS: baseline, workstream/status/search filters, date sorting, drafts, reload, SQL saves, additions, rollback UI, XSS escaping, concurrency conflict, draft export, light/dark, desktop/mobile, dialogs.');
    await context.close();
  } finally {
    if (browser) await browser.close();
    server.kill();
    await new Promise(resolve => server.once('exit', resolve));
    fs.rmSync(temporary, {recursive: true, force: true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
