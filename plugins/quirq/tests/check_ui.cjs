// Optional browser smoke check: NODE_PATH must resolve playwright.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1000, height: 740 } });
    await page.goto('http://127.0.0.1:5014/dashboard.html?demo=1');
    await page.getByRole('button', { name: /Sample app/ }).click();
    await page.getByRole('heading', { name: 'Todos (2)' }).waitFor();
    assert.equal(await page.locator('#context').textContent(), 'Demo selection: sample-app');
    await page.getByRole('searchbox').fill('research');
    assert.equal(await page.locator('.project').count(), 1);
    await page.getByRole('searchbox').fill('');
    if (process.argv[2]) await page.screenshot({ path: process.argv[2], fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    // Emulate an MCP Apps parent host; exercise actual iframe bridge requests.
    await page.setViewportSize({ width: 1000, height: 740 });
    await page.setContent(`<iframe style="width:950px;height:700px" src="http://127.0.0.1:5014/dashboard.html"></iframe>`);
    await page.evaluate(() => {
      window.shared = null;
      addEventListener('message', event => {
        const m = event.data;
        if (!m || m.jsonrpc !== '2.0' || m.id === undefined || !m.method) return;
        let result = {};
        if (m.method === 'ui/initialize') result = { protocolVersion: '2026-01-26', hostInfo: { name: 'test', version: '1' }, hostCapabilities: { updateModelContext: {} }, hostContext: { availableDisplayModes: ['inline', 'fullscreen'] } };
        else if (m.method === 'tools/call') {
          const data = m.params.name === 'space_dashboard'
            ? { items: [{ id: 'sample', display_name: 'Sample', description: '<img src=x onerror=alert(1)>' }] }
            : { project: { id: 'sample', display_name: 'Sample' }, todos: [{ content: '<script>bad()</script>', status: 'pending' }], sessions: [] };
          result = { structuredContent: data, content: [] };
        } else if (m.method === 'ui/update-model-context') window.shared = m.params.structuredContent;
        event.source.postMessage({ jsonrpc: '2.0', id: m.id, result }, '*');
      });
      // Reload once after the listener is installed.
      document.querySelector('iframe').src = 'http://127.0.0.1:5014/dashboard.html?host-test=1';
    });
    const frame = page.frameLocator('iframe');
    await frame.getByRole('button', { name: /Sample <img/ }).click();
    await frame.getByRole('heading', { name: 'Todos (1)' }).waitFor();
    await frame.locator('#context').filter({ hasText: 'selection shared' }).waitFor();
    assert.deepEqual(await page.evaluate(() => window.shared), { project_id: 'sample' });
    assert.equal(await frame.locator('#detail script').count(), 0);
    assert.equal(await frame.locator('#projects img').count(), 0);
    await frame.getByRole('button', { name: 'Fullscreen' }).click();
    console.log('UI checks passed: demo selection, filtering, mobile width, MCP initialization/tools/context, escaped content, fullscreen request.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
