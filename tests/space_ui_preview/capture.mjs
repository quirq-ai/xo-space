#!/usr/bin/env node
/* Real-browser issue #100 verification against server.py's fictional data.
   No DOM, CSS, asset or response substitutions: screenshots show the app.
   Install Playwright normally, or provide PLAYWRIGHT_MODULE=/path/to/index.mjs. */
import assert from 'node:assert/strict';
import {mkdir, writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const {chromium} = await import(process.env.PLAYWRIGHT_MODULE
  ? pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href : 'playwright');
const origin = process.env.SPACE_PREVIEW_URL || 'http://127.0.0.1:5100';
const output = resolve(process.argv[2] || '/tmp/space-ui-issue-100');
const screenshotsOnly = process.argv.includes('--screenshots-only');
await mkdir(output, {recursive: true});

const browser = await chromium.launch({headless: true,
  ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? {executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE} : {})});
const context = await browser.newContext({viewport: {width: 1440, height: 1000},
  deviceScaleFactor: 1, locale: 'en-US', timezoneId: 'UTC', reducedMotion: 'reduce'});
await context.addInitScript(() => {
  // Keep relative labels and randomized graph starting positions repeatable.
  Date.now = () => Date.parse('2026-09-14T10:00:00Z');
  let seed = 100;
  Math.random = () => ((seed = (1664525 * seed + 1013904223) >>> 0) / 4294967296);
});
const page = await context.newPage();
const errors = [];
const requests = [];
page.on('pageerror', e => errors.push(e.message));
page.on('console', message => { if(message.type() === 'error') errors.push(message.text()); });
page.on('response', response => {
  if(response.status() >= 400) errors.push(`${response.status()} ${response.url()}`);
});
page.on('request', request => {
  if(request.isNavigationRequest() && request.frame() === page.mainFrame()) requests.push(request.url());
});

async function settleGraph() {
  await page.waitForFunction(() => document.querySelector('#q')?.placeholder.match(/Search \d+/));
  await page.waitForFunction(() => document.querySelector('#simstat')?.style.opacity === '0', undefined, {timeout: 20000});
}
async function screenshot(name) {
  await page.mouse.move(1425, 970);
  await page.screenshot({path: resolve(output, name), animations: 'disabled'});
}
async function lens(id) {
  await page.locator(`[data-files-lens="${id}"]`).click();
  await page.waitForFunction(id => location.hash === '#/' + id
    && document.querySelector(`[data-files-lens="${id}"]`)?.getAttribute('aria-current') === 'true', id);
  await page.waitForLoadState('networkidle');
  if(id === 'projects') await page.locator('.prj-row').first().waitFor();
  if(id === 'sharing') await page.locator('.shl-detail').waitFor();
  if(id === 'tree') await page.locator('#view-tree').waitFor({state: 'visible'});
  if(id === 'dashboard' || id === 'graph') await settleGraph();
}
const report = {workspace: 'Fictional fixture data only', screenshots: [], checks: [], errors};

try {
  await page.goto(origin + '/space/', {waitUntil: 'networkidle'});
  await settleGraph();
  if(!screenshotsOnly) {
    assert.equal(new URL(page.url()).hash, '#/dashboard', 'Dashboard is the initial view');
    assert.deepEqual(await page.locator('.tabs button').evaluateAll(buttons => buttons.map(b => b.id)),
      ['tab-projects', 'tab-time', 'tab-sessions', 'tab-inbox', 'tab-secrets', 'tab-connectors']);
    assert.deepEqual(await page.locator('[data-files-lens]').allTextContents(),
      ['Dashboard', 'List', 'Graph', 'Tree', 'Sharing']);
    assert.equal(await page.locator('#tab-projects').textContent(), 'Projects');
    report.checks.push('Default Dashboard; exact six-tab order; five Projects lenses');
  }
  await screenshot('space-dashboard.png');
  report.screenshots.push('space-dashboard.png');

  if(screenshotsOnly) {
    await page.goto(origin + '/space/#/projects', {waitUntil: 'networkidle'});
    await page.locator('.prj-row').first().waitFor();
  } else await lens('projects');
  assert.equal(await page.locator('.prj-row').count(), 10);
  await screenshot('projects-list.png');
  report.screenshots.push('projects-list.png');
  await page.locator('[data-id="aurora-console"].prj-row-head').click();
  await page.locator('[data-file="README.md"]').waitFor();
  await screenshot('projects-detail.png');
  report.screenshots.push('projects-detail.png');

  if(!screenshotsOnly) {
    await page.locator('[data-file="README.md"]').click();
    await page.locator('#preview-body .pv-md').waitFor();
    await page.locator('#preview-version').selectOption('0');
    await page.waitForFunction(() => document.querySelector('#preview-body')?.textContent.includes('An earlier version'));
    await page.locator('#preview-source').click();
    const beforeNavigations = requests.length;
    const beforeContent = await page.locator('#preview-body').textContent();
    const beforeLens = await page.locator('#fileslens').boundingBox();
    for(const id of ['dashboard', 'graph', 'tree', 'sharing', 'projects', 'dashboard', 'graph']) {
      await lens(id);
      await page.waitForFunction(content => document.querySelector('#preview-body')?.textContent === content, beforeContent);
      assert.equal(await page.locator('#preview').evaluate(el => el.classList.contains('is-open')), true, `${id} keeps the preview open`);
      assert.equal(await page.locator('#preview-body').textContent(), beforeContent, `${id} keeps the same version/content`);
      assert.equal(await page.locator('#preview-version').inputValue(), '0', `${id} keeps version selection`);
      assert.equal(await page.locator('#preview-source').textContent(), 'Rendered', `${id} keeps source mode`);
      const bounds = await page.locator('#fileslens').boundingBox();
      for(const key of ['x', 'y', 'width', 'height']) assert.ok(Math.abs(bounds[key] - beforeLens[key]) < 1, `${id} lens switch ${key} stays fixed`);
    }
    assert.ok(requests.length > beforeNavigations, 'Dashboard/Graph switches exercise actual dataset reloads');
    report.checks.push('Versioned source preview survives every Projects lens, including dataset reloads; stable switch position');
    const beforeWikiNavigations = requests.length;
    await page.locator('#wiki-link').click();
    await page.waitForFunction(() => location.hash === '#/wiki');
    await page.locator('#view-wiki').waitFor({state: 'visible'});
    assert.equal(context.pages().length, 1, 'Wiki opens in the same tab');
    assert.equal(requests.length, beforeWikiNavigations, 'Wiki uses local hash navigation');
    await page.locator('#wiki-link[aria-current="page"]').waitFor();
    assert.equal(await page.locator('.tabs .is-on').count(), 0, 'Wiki selects no primary tab');
    assert.equal(await page.locator('#fileslens').isHidden(), true);
    assert.equal(await page.locator('#preview').evaluate(el => el.classList.contains('is-open')), false);
    report.checks.push('Wiki resource opens locally, marks itself active and closes the Projects preview');
    await page.locator('#tab-projects').click();
    await page.waitForFunction(() => location.hash === '#/projects');
    await page.waitForFunction(() => !document.querySelector('#wiki-link').hasAttribute('aria-current'));
    report.checks.push('Top-level Projects preserves the List route');

    for(const id of ['dashboard', 'projects', 'graph', 'tree', 'sharing']) {
      await page.goto(origin + '/space/#/' + id, {waitUntil: 'networkidle'});
      await page.waitForFunction(id => document.querySelector(`[data-files-lens="${id}"]`)?.getAttribute('aria-current') === 'true', id);
      assert.equal(await page.locator('#tab-projects').evaluate(el => el.classList.contains('is-on')), true, `${id} deep link selects Projects`);
    }
    report.checks.push('Every existing Projects lens deep link selects the correct tab and lens');

    await page.goto(origin + '/space/#/wiki', {waitUntil: 'networkidle'});
    await page.locator('#view-wiki').waitFor({state: 'visible'});
    await page.locator('#wiki-link[aria-current="page"]').waitFor();
    assert.equal(await page.locator('.tabs .is-on').count(), 0);
    report.checks.push('Wiki deep link remains routable without a primary tab');

    const tabIds = ['projects', 'time', 'sessions', 'inbox', 'secrets', 'connectors'];
    for(const [index, id] of tabIds.entries()) {
      await page.locator('body').click({position: {x: 3, y: 3}});
      await page.keyboard.press(String(index + 1));
      await page.waitForFunction(id => location.hash === '#/' + id, id);
      await page.waitForLoadState('networkidle');
      assert.equal(await page.locator('#tab-' + id).evaluate(el => el.classList.contains('is-on')), true);
    }
    await page.keyboard.press('7');
    assert.equal(new URL(page.url()).hash, '#/connectors', 'Only six primary tabs consume number keys');
    report.checks.push('Number keys 1–6 select Projects, Timeline, Sessions, Inbox, Setup, Connectors');

    for(const width of [375, 320]) {
      await page.setViewportSize({width, height: 900});
      await page.goto(origin + '/space/#/projects', {waitUntil: 'networkidle'});
      await page.locator('.prj-row').first().waitFor();
      const initialBounds = await page.locator('#fileslens').boundingBox();
      for(const id of ['dashboard', 'projects', 'graph', 'tree', 'sharing']) {
        const button = page.locator(`[data-files-lens="${id}"]`);
        await button.scrollIntoViewIfNeeded();
        assert.equal(await button.isVisible(), true, `${id} lens is reachable at ${width}px`);
        await lens(id);
        const bounds = await page.locator('#fileslens').boundingBox();
        for(const key of ['x', 'y', 'width', 'height']) assert.ok(Math.abs(bounds[key] - initialBounds[key]) < 1, `${id} lens ${key} stays fixed at ${width}px`);
        const size = await page.evaluate(() => ({width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth}));
        assert.ok(size.scroll <= size.width, `${id} at ${width}px has no document overflow (${size.scroll})`);
      }
      await lens('projects');
      const row = page.locator('.prj-row').first();
      const files = await row.locator('.prj-num').boundingBox();
      const active = await row.locator('.prj-when').boundingBox();
      assert.ok(files.y + files.height <= active.y, `File counts and activity do not overlap at ${width}px`);
      const footer = await page.locator('footer').boundingBox();
      const status = await page.locator('footer .srv').boundingBox();
      assert.ok(status.y >= footer.y && status.y + status.height <= footer.y + footer.height,
        `Server status stays inside the footer at ${width}px`);
      await screenshot(`projects-mobile-${width}.png`);
      report.screenshots.push(`projects-mobile-${width}.png`);
    }
    report.checks.push('320px and 375px: stable lens bounds, no page overflow, readable project counts/activity and footer');
  }
  assert.deepEqual(errors, [], 'No console errors, uncaught exceptions or HTTP failures');
  report.checks.push('No browser console, page or HTTP errors');
  console.log(JSON.stringify(report, null, 2));
} catch(error) {
  report.failure = error.stack;
  await page.screenshot({path: resolve(output, 'failure.png')}).catch(() => {});
  throw error;
} finally {
  await writeFile(resolve(output, 'report.json'), JSON.stringify(report, null, 2));
  await browser.close();
}
