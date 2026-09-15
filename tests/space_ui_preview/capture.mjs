#!/usr/bin/env node
/* Real-browser issue #100 verification against server.py's fictional data.
   No DOM, CSS, asset or response substitutions: screenshots show the app.
   Install Playwright normally, or provide PLAYWRIGHT_MODULE=/path/to/index.mjs. */
import assert from 'node:assert/strict';
import {routeFor,projectPageSelector,openProjectPage} from './routes.mjs';
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
  await page.waitForFunction(() => /\d+/.test(document.querySelector('#counts')?.textContent || ''));
  await page.waitForFunction(() => document.querySelector('#simstat')?.style.opacity === '0', undefined, {timeout: 20000});
}
async function screenshot(name) {
  await page.mouse.move(1425, 970);
  await page.screenshot({path: resolve(output, name), animations: 'disabled'});
}
async function lens(id) {
  await openProjectPage(page,id);
  await page.waitForFunction(({route,selector}) => location.hash === route
    && document.querySelector(selector)?.getAttribute('aria-current') === 'page', {route:routeFor(id),selector:projectPageSelector(id)});
  await page.waitForLoadState('networkidle');
  if(id === 'projects') await page.locator('.prj-row').first().waitFor();
  if(id === 'sharing') await page.locator('.shl-detail').waitFor();
  if(id === 'tree') await page.locator('#view-tree').waitFor({state: 'visible'});
  if(id === 'dashboard' || id === 'graph') await settleGraph();
}
const report = {workspace: 'Fictional fixture data only', screenshots: [], checks: [], layouts: [], errors};

async function projectChrome() {
  const root = await page.locator('#graph-root').elementHandle();
  const rootButton = await page.locator('#root-btn').elementHandle();
  const rootInput = await page.locator('#root-q').elementHandle();
  const sameRoot = async () => {
    for(const [handle, id] of [[root, 'graph-root'], [rootButton, 'root-btn'], [rootInput, 'root-q']])
      assert.equal(await handle.evaluate((node, id) => node === document.getElementById(id), id), true,
        id + ' remains the original connected control');
    assert.equal(await page.locator('.topbar #graph-root').count(), 0, 'Graph root is outside the topbar');
  };
  for(const width of [1440, 390, 320]) {
    await page.setViewportSize({width, height: 1000});
    for(const id of ['dashboard', 'graph']) {
      await lens(id);await sameRoot();
      const bounds = await page.evaluate(() => {
        const rect = node => {const r = node.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,height:r.height};};
        const nav = document.querySelector('#section-nav');
        return {root:rect(document.querySelector('#root-btn')), refresh:rect(document.querySelector('#project-refresh')),
          nav:rect(nav), row:rect(nav.querySelector('.section-nav-inner')), graph:rect(document.querySelector('#view-graph')),
          canvas:rect(document.querySelector('#gcanvas')), scroll:document.documentElement.scrollWidth,
          context:[...document.querySelectorAll('.section-page-context')].some(node=>node.getClientRects().length)};
      });
      report.layouts.push({page:id,width,...bounds});
      assert.equal(await page.locator('#section-nav #graph-root').count(), 1, 'Projects navigation owns the root picker');
      assert.ok(bounds.root.right <= bounds.refresh.left + 1 && Math.abs(bounds.root.top - bounds.refresh.top) < 4,
        id + ' root sits immediately left of Refresh at ' + width);
      assert.ok(bounds.root.left >= 0 && bounds.refresh.right <= width + 1 && bounds.scroll <= width,
        id + ' actions fit the viewport at ' + width);
      assert.equal(bounds.context, false, id + ' has no visible context hero');
      assert.ok(bounds.nav.bottom - bounds.row.bottom < 3, id + ' navigation reserves no space for a removed hero');
      assert.ok(Math.abs(bounds.graph.top - bounds.nav.bottom) < 2 && bounds.canvas.height >= bounds.graph.height - 2,
        id + ' canvas uses the full area directly below navigation');
      const original = await page.locator('#root-name').textContent();
      await page.locator('#root-btn').click();await page.locator('#rootdd.is-open').waitFor();
      await page.locator('#root-q').fill('Aurora Console');
      const result = page.locator('#root-ac button').filter({hasText:'Aurora Console'}).first();
      await result.waitFor();
      const dropdown = await page.locator('#rootdd').boundingBox();
      assert.ok(dropdown.x >= -1 && dropdown.x + dropdown.width <= width + 1,
        id + ' root dropdown fits at ' + width);
      for(const selector of ['#root-q', '#root-ac button', '#root-reset']) {
        const hit = await page.locator(selector).first().evaluate(async node => {
          const read = () => {
            const r=node.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;
            const describe=el=>{const css=getComputedStyle(el);return{tag:el.tagName,id:el.id,className:el.className,
              pointerEvents:css.pointerEvents,zIndex:css.zIndex,visibility:css.visibility};};
            const stack=document.elementsFromPoint(x,y),top=stack[0];
            return{ok:node===top||node.contains(top),x,y,stack:stack.map(describe),
              controls:[node,document.querySelector('#section-nav'),document.querySelector('#graph-file-toolbar')].filter(Boolean).map(describe)};
          };
          const first=read();if(first.ok)return first;
          await new Promise(resolve=>requestAnimationFrame(resolve));return{...first,nextFrame:read()};
        });
        if(!hit.ok)report.layouts.push({page:id,width,selector,hit});
        assert.equal(hit.ok,true,id+' '+selector+' is not clipped by the navigation: '+JSON.stringify(hit));
      }
      const name = id + '-root-' + width + '.png';
      await screenshot(name);report.screenshots.push(name);
      await page.locator('#root-q').press('Enter');
      await page.waitForFunction(()=>document.querySelector('#root-name').textContent==='Aurora Console');
      await page.locator('#root-btn').click();await page.locator('#rootdd.is-open').waitFor();
      await page.locator('#root-reset').click();
      await page.waitForFunction(label=>document.querySelector('#root-name').textContent===label,original);
      assert.equal(await page.locator('#rootdd.is-open').count(), 0, 'Reset closes the root picker');
    }
    for(const id of ['tree','projects','manage']) {
      await lens(id);await sameRoot();
      assert.equal(await page.locator('#root-btn').isVisible(),true,id+' retains the shared Projects root picker');
      if(id!=='projects')assert.equal(await page.locator('.section-page-context:visible').count(),0,id+' has no visible context hero');
    }
  }
  await page.setViewportSize({width:1440,height:1000});
  for(const id of ['agents','inbox','setup']) {
    await page.locator('#tab-'+id).click();await page.locator('#view-'+id+'.is-active').waitFor();
    await sameRoot();
    await page.locator('#tab-projects').click();await settleGraph();await sameRoot();
    await page.locator('#root-btn').click();await page.locator('#rootdd.is-open').waitFor();
    await page.locator('#root-btn').click();
    assert.equal(await page.locator('#rootdd.is-open').count(),0,'One click toggles once after '+id+' navigation');
  }
  report.checks.push('Projects root picker stays beside Refresh, preserves its DOM and listeners across sections, reroots and resets both maps, and fits unclipped at 1440/390/320px without context heroes.');
}

try {
  await page.goto(origin + '/space/', {waitUntil: 'networkidle'});
  await settleGraph();
  if(!screenshotsOnly) {
    assert.equal(new URL(page.url()).hash, '#/projects/overview', 'Projects Overview is the initial view');
    assert.deepEqual(await page.locator('.tabs a').evaluateAll(buttons => buttons.map(b => b.id)),
      ['tab-projects', 'tab-agents', 'tab-inbox', 'tab-setup']);
    assert.deepEqual(await page.locator('[data-section-page]').allTextContents(),
      ['Overview', 'Files', 'Sharing', 'Timeline']);
    assert.equal(await page.locator('.section-nav-label:visible').count(),0,'Navigation does not repeat the primary section label');
    assert.equal(await page.locator('#tab-projects').textContent(), 'Projects');
    report.checks.push('Default Overview; four Projects pages with Data grouping the original List, Graph and Tree');
    await projectChrome();
  }
  await screenshot('space-dashboard.png');
  report.screenshots.push('space-dashboard.png');

  if(screenshotsOnly) {
    await page.goto(origin + '/space/#/projects/data/list', {waitUntil: 'networkidle'});
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

    const beforeContent = await page.locator('#preview-body').textContent();
    const beforeLens = await page.locator('#section-nav').boundingBox();
    for(const id of ['dashboard', 'graph', 'tree', 'time', 'manage', 'projects', 'dashboard', 'graph']) {
      await lens(id);
      await page.waitForFunction(content => document.querySelector('#preview-body')?.textContent === content, beforeContent);
      assert.equal(await page.locator('#preview').evaluate(el => el.classList.contains('is-open')), true, `${id} keeps the preview open`);
      assert.equal(await page.locator('#preview-body').textContent(), beforeContent, `${id} keeps the same version/content`);
      assert.equal(await page.locator('#preview-version').inputValue(), '0', `${id} keeps version selection`);
      assert.equal(await page.locator('#preview-source').textContent(), 'Rendered', `${id} keeps source mode`);
      const bounds = await page.locator('#section-nav').boundingBox();
      for(const key of ['x', 'y', 'width']) assert.ok(Math.abs(bounds[key] - beforeLens[key]) < 1, `${id} secondary navigation ${key} stays fixed`);
      if(id === 'dashboard' || id === 'graph') {
        await page.locator('#root-btn').click();await page.locator('#rootdd.is-open').waitFor();
        await page.locator('#root-q').fill('Aurora');
        await page.locator('#root-ac.is-open').waitFor();
        assert.equal(await page.locator('#root-ac button').first().evaluate(node => {
          const r=node.getBoundingClientRect(),top=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
          return node===top||node.contains(top);
        }),true,`${id} root results remain above the open file preview`);
        // Escape intentionally closes a file preview first. Toggle the picker
        // so this check keeps the historical preview open across map changes.
        await page.locator('#root-btn').click();
        assert.equal(await page.locator('#rootdd.is-open').count(), 0, `${id} root picker stays usable with a file preview open`);
      }
    }
    report.checks.push('Versioned source preview survives every Projects lens, with a stable navigation position');
    const beforeWikiNavigations = requests.length;
    await page.locator('#wiki-link').click();
    await page.waitForFunction(() => location.hash === '#/wiki');
    await page.locator('#view-wiki').waitFor({state: 'visible'});
    assert.equal(context.pages().length, 1, 'Wiki opens in the same tab');
    assert.equal(requests.length, beforeWikiNavigations, 'Wiki uses local hash navigation');
    await page.locator('#wiki-link[aria-current="page"]').waitFor();
    assert.equal(await page.locator('.tabs .is-on').count(), 0, 'Wiki selects no primary tab');
    assert.equal(await page.locator('#section-nav').isHidden(), true);
    assert.equal(await page.locator('#preview').evaluate(el => el.classList.contains('is-open')), false);
    report.checks.push('Wiki resource opens locally, marks itself active and closes the Projects preview');
    await page.locator('#tab-projects').click();
    await page.waitForFunction(() => location.hash === '#/projects/overview');
    await page.waitForFunction(() => !document.querySelector('#wiki-link').hasAttribute('aria-current'));
    report.checks.push('Primary Projects opens its Overview default');

    for(const id of ['dashboard', 'projects', 'graph', 'tree', 'sharing', 'time', 'manage']) {
      await page.goto(origin + '/space/' + routeFor(id), {waitUntil: 'networkidle'});
      await page.waitForFunction(selector => document.querySelector(selector)?.getAttribute('aria-current') === 'page', projectPageSelector(id));
      assert.equal(await page.locator(id==='sharing'?'#tab-inbox':'#tab-projects').evaluate(el => el.classList.contains('is-on')), true, `${id} deep link selects its section`);
    }
    report.checks.push('Every existing Projects page deep link selects the correct tab and lens');

    await page.goto(origin + '/space/#/agents/overview', {waitUntil: 'networkidle'});
    await page.locator('#view-agents').waitFor({state: 'visible'});
    assert.equal(await page.locator('#tab-agents.is-on').count(), 1);
    assert.equal(await page.locator('#tab-agents').textContent(), 'Agents');
    assert.equal(await page.locator('#section-nav [data-section-page="agents-overview"]').getAttribute('aria-current'),'page');
    report.checks.push('Agents deep link opens agent telemetry under the renamed primary tab');

    await page.goto(origin + '/space/#/wiki', {waitUntil: 'networkidle'});
    await page.locator('#view-wiki').waitFor({state: 'visible'});
    await page.locator('#wiki-link[aria-current="page"]').waitFor();
    assert.equal(await page.locator('.tabs .is-on').count(), 0);
    report.checks.push('Wiki deep link remains routable without a primary tab');

    const tabIds = ['projects', 'agents', 'inbox', 'setup'];
    for(const [index, id] of tabIds.entries()) {
      await page.locator('body').click({position: {x: 3, y: 3}});
      await page.keyboard.press(String(index + 1));
      await page.waitForFunction(hash => location.hash === hash, id==='projects'?'#/projects/overview':routeFor(id));
      await page.waitForLoadState('networkidle');
      assert.equal(await page.locator('#tab-' + id).evaluate(el => el.classList.contains('is-on')), true);
    }
    await page.keyboard.press('5');
    assert.equal(new URL(page.url()).hash, '#/setup/workspace', 'Only four primary tabs consume number keys');
    report.checks.push('Number keys 1–4 select Projects, Agents, Inbox and Setup');

    for(const width of [375, 320]) {
      await page.setViewportSize({width, height: 900});
      await page.goto(origin + '/space/#/projects/data/list', {waitUntil: 'networkidle'});
      await page.locator('.prj-row').first().waitFor();
      const initialBounds = await page.locator('#section-nav').boundingBox();
      const initialStage = await page.locator('#stage').boundingBox();
      for(const id of ['dashboard', 'projects', 'graph', 'tree', 'sharing', 'time', 'manage']) {
        await lens(id);
        const button = page.locator(projectPageSelector(id));
        await button.scrollIntoViewIfNeeded();
        assert.equal(await button.isVisible(), true, `${id} lens is reachable at ${width}px`);
        const bounds = await page.locator('#section-nav').boundingBox();
        for(const key of ['x', 'width']) assert.ok(Math.abs(bounds[key] - initialBounds[key]) < 1, `${id} lens ${key} stays fixed at ${width}px`);
        const stageBounds = await page.locator('#stage').boundingBox();
        assert.ok(Math.abs((bounds.y - stageBounds.y) - (initialBounds.y - initialStage.y)) < 1,
          `${id} lens stays anchored to the content at ${width}px as the contextual header changes height`);
        const size = await page.evaluate(() => ({width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth}));
        assert.ok(size.scroll <= size.width, `${id} at ${width}px has no document overflow (${size.scroll})`);
      }
      await lens('projects');
      const row = page.locator('.prj-row').first();
      const files = await row.locator('.prj-num').boundingBox();
      const active = await row.locator('.prj-when').boundingBox();
      const overlaps=Math.min(files.x+files.width,active.x+active.width)>Math.max(files.x,active.x)+1
        &&Math.min(files.y+files.height,active.y+active.height)>Math.max(files.y,active.y)+1;
      assert.equal(overlaps,false, `File counts and activity do not overlap at ${width}px`);
      await screenshot(`projects-mobile-${width}.png`);
      report.screenshots.push(`projects-mobile-${width}.png`);
    }
    report.checks.push('320px and 375px: lenses anchored below the contextual header, no page overflow, readable project counts/activity');
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
