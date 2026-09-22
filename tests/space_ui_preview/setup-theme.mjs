/* Theme preferences are fictional and browser-owned. Every mutation is
   intercepted before it can reach the read-only preview server. */
import assert from 'node:assert/strict';
import {installRefreshProbes,startDataRefresh} from './refresh-helpers.mjs';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5100';
const endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');
assert.ok(!['5002','5112'].includes(endpoint.port),'Use the fictional preview server');
const output=resolve(process.argv[2]||'/tmp/space-setup-theme');
await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE
  ?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const browser=await chromium.launch({headless:true,
  executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
const page=await context.newPage();page.setDefaultTimeout(15000);
const report={origin,checks:[],requests:[],writes:[],errors:[],screenshots:[],layouts:[],styles:[]};
const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZrS8AAAAASUVORK5CYII=','base64');
const branding={name:'Aurora Studio',logo_url:'/space/branding/logo?v=abcdef1234567890'};
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
const gate=()=>({arrived:deferred(),release:deferred(),finished:deferred()});
let saved={theme:'space'},saveError=null,readError=null,holdSave=null,holdRead=null;
const pendingGates=[];
const send=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),path=url.pathname,method=request.method();
  report.requests.push({path,method});
  if(url.origin!==endpoint.origin){report.errors.push('Blocked external request '+request.url());return route.abort();}
  if(path==='/space/theme'){
    if(method==='GET'){
      const snapshot=structuredClone(saved),pending=holdRead;holdRead=null;
      if(pending){pending.arrived.resolve();await pending.release.promise;}
      await send(route,readError?{detail:readError}:snapshot,readError?503:200);
      pending?.finished.resolve();return;
    }
    if(method==='PUT'){
      assert.match(request.headers()['content-type'],/^application\/json/);
      const body=request.postDataJSON();report.writes.push(body);
      assert.deepEqual(Object.keys(body),['theme'],'Only the theme preference is submitted');
      assert.ok(['space','quirq','midnight','graphite','linen'].includes(body.theme));
      const pending=holdSave;holdSave=null;
      if(pending){pending.arrived.resolve();await pending.release.promise;}
      if(saveError)return send(route,{detail:saveError},503);
      saved=body;return send(route,saved);
    }
  }
  if(path==='/xo/sessions.json'&&method==='GET')return send(route,{
    meta:{sources:[{id:'demo',label:'Fictional telemetry',available:true}]},
    totals:{sessions:0,sessions_by_agent:{demo:0}},sessions:[],daily_sessions:[],daily_tools:[],
    daily_models:[{agent:'demo',day:new Date().toISOString().slice(0,10),model:'Demo model',tokens:1500,cost:0,cost_known:false}],
  });
  if(path==='/space/branding'&&method==='GET')return send(route,branding);
  if(path==='/space/branding/logo'&&method==='GET')return route.fulfill({contentType:'image/png',body:png});
  if(method!=='GET'){
    report.errors.push('Blocked unexpected write '+method+' '+path);
    return send(route,{detail:'Fixture blocked unexpected write'},403);
  }
  return route.continue();
});
await installRefreshProbes(context);
page.on('pageerror',error=>report.errors.push(error.message));
page.on('console',message=>{
  if(message.type()!=='error')return;
  if(new URL(message.location().url||origin).pathname==='/space/theme'&&message.text().includes('503'))return;
  report.errors.push(message.text()+' '+message.location().url);
});
page.on('response',response=>{
  if(response.status()<400)return;
  if(new URL(response.url()).pathname==='/space/theme'&&response.status()===503)return;
  report.errors.push(response.status()+' '+response.url());
});
const card=page.locator('#setup-theme'),save=page.locator('#theme-save');
const select=page.locator('#theme-select');
const checked=text=>{report.checks.push(text);console.log(text);};
async function arrived(promise){
  let timer;
  try{await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Held theme request did not arrive')),15000);})]);}
  finally{clearTimeout(timer);}
}
async function currentTheme(expected){
  await page.waitForFunction(expected=>document.documentElement.dataset.theme===expected,expected);
}
async function idle(){await page.waitForFunction(()=>document.querySelector('#theme-form')?.getAttribute('aria-busy')==='false');}
async function expectBranding(){
  assert.equal(await page.locator('.brand b').textContent(),branding.name);
  assert.equal(await page.locator('.brand .mark img').getAttribute('src'),branding.logo_url);
  assert.equal(await page.locator('#branding-name').inputValue(),branding.name);
  assert.equal(await page.title(),branding.name);
  assert.ok(report.requests.every(r=>r.path!=='/space/branding'||r.method==='GET'),'Theme never writes branding');
}
async function screenshot(label,scrollCard=true){
  if(scrollCard)await card.scrollIntoViewIfNeeded();
  await page.screenshot({path:resolve(output,label),animations:'disabled'});
  report.screenshots.push(label);
}
async function assertStyles(theme){
  await page.evaluate(async()=>{await document.fonts.ready;});
  const styles=await page.evaluate(()=>{
    const css=getComputedStyle(document.documentElement);
    return{theme:document.documentElement.dataset.theme,bg:getComputedStyle(document.body).backgroundColor,
      ink:getComputedStyle(document.body).color,accent:css.getPropertyValue('--accent').trim(),
      bodyFont:getComputedStyle(document.body).fontFamily,brandFont:getComputedStyle(document.querySelector('.brand b')).fontFamily,
      mono:css.getPropertyValue('--mono'),active:getComputedStyle(document.querySelector('.tabs .is-on')).backgroundColor,
      activeInk:getComputedStyle(document.querySelector('.tabs .is-on')).color,
      fonts:[...document.fonts].filter(f=>f.status==='loaded').map(f=>f.family)};
  });
  report.styles.push(styles);
  assert.equal(styles.theme,theme);assert.match(styles.bodyFont,/Inter/);
  assert.ok(styles.fonts.some(f=>f.includes('Inter')),'Inter is loaded from bundled assets');
  if(theme==='space'){
    assert.equal(styles.bg,'rgb(11, 12, 15)');assert.equal(styles.accent,'#a8d94f');
    assert.equal(styles.active,'rgb(168, 217, 79)');assert.match(styles.brandFont,/Inter/);
  }else{
    assert.equal(styles.bg,'rgb(16, 15, 20)');assert.equal(styles.ink,'rgb(243, 236, 228)');
    assert.equal(styles.active,'rgb(242, 162, 213)');assert.equal(styles.activeInk,'rgb(33, 20, 30)');
    assert.match(styles.brandFont,/Poppins/);assert.match(styles.mono,/JetBrains Mono/);
    assert.ok(styles.fonts.some(f=>f.includes('Poppins')),'Poppins brand typeface is loaded');
    const monoLoaded=await page.evaluate(async()=>{
      const fonts=await document.fonts.load('400 12px "JetBrains Mono"');
      return fonts.some(font=>font.family.includes('JetBrains Mono')&&font.status==='loaded');
    });
    assert.ok(monoLoaded,'Bundled JetBrains Mono can load without an external request');
  }
}

try{
  await page.goto(origin+'/space/#/setup/workspace',{waitUntil:'networkidle'});
  await card.waitFor();await idle();await currentTheme('space');
  assert.equal(await page.locator('#setup-panel-workspace #setup-theme').count(),1);
  assert.deepEqual(await select.locator('option').allTextContents(),['Grove — default','Neon','Midnight','Graphite','Linen — light']);
  assert.equal((await select.inputValue()==='space'),true);assert.equal(await save.isDisabled(),true);
  assert.deepEqual(report.writes,[]);await expectBranding();await assertStyles('space');
  await screenshot('theme-space-1440.png');
  checked('Workspace owns Theme; the original green Grove theme and custom branding load without a write.');

  await select.selectOption('quirq');saved={theme:'quirq'};
  const externalRead=holdRead=gate();pendingGates.push(externalRead);
  await startDataRefresh(page,'setup');await arrived(externalRead.arrived.promise);
  await select.selectOption('space');externalRead.release.resolve();await arrived(externalRead.finished.promise);await idle();
  await currentTheme('quirq');assert.equal((await select.inputValue()==='space'),true);
  assert.equal(await save.isEnabled(),true,'A newer draft is compared with the refreshed saved theme');
  assert.match(await page.locator('#theme-status').textContent(),/Unsaved changes/);
  await save.click();await currentTheme('space');await idle();
  assert.deepEqual(saved,{theme:'space'});await expectBranding();
  checked('A selection made during an externally changed refresh remains a saveable draft against the new baseline.');

  const writesBeforeQuirq=report.writes.length;
  await select.selectOption('quirq');
  assert.equal(await page.locator('html').getAttribute('data-theme'),'space','Selecting a preview is an unsaved draft');
  assert.match(await page.locator('#setup-step-workspace').textContent(),/Unsaved changes/);
  await page.locator('#tab-projects').click();await page.locator('#tab-setup').click();
  await card.waitFor();await idle();assert.equal((await select.inputValue()==='quirq'),true);
  await startDataRefresh(page,'setup');await idle();
  assert.equal((await select.inputValue()==='quirq'),true,'Internal status rereads preserve the draft selection');
  assert.equal(report.writes.length,writesBeforeQuirq);await expectBranding();
  checked('Theme drafts survive navigation and internal status rereads and keep the Workspace unsaved badge without changing the shell.');

  const stale=holdRead=gate();pendingGates.push(stale);
  await startDataRefresh(page,'setup');await arrived(stale.arrived.promise);
  const pending=holdSave=gate();pendingGates.push(pending);
  await save.click();await arrived(pending.arrived.promise);
  assert.equal(await save.isDisabled(),true);assert.equal(await select.isDisabled(),true);
  assert.equal(report.writes.length,writesBeforeQuirq+1);await currentTheme('space');
  pending.release.resolve();await currentTheme('quirq');
  stale.release.resolve();await arrived(stale.finished.promise);await idle();
  assert.equal((await select.inputValue()==='quirq'),true);await currentTheme('quirq');
  assert.deepEqual(saved,{theme:'quirq'});assert.deepEqual(report.writes.at(-1),{theme:'quirq'});
  assert.doesNotMatch(await page.locator('#setup-step-workspace').textContent(),/Unsaved changes/);
  await expectBranding();await assertStyles('quirq');
  await page.reload({waitUntil:'networkidle'});await card.waitFor();await idle();await currentTheme('quirq');
  assert.equal((await select.inputValue()==='quirq'),true);await expectBranding();
  checked('Save applies Neon immediately, blocks duplicate saves, survives reload, and ignores a stale GET arriving after save.');

  saveError='Theme could not be saved. Please try again.';
  await select.selectOption('space');await save.click();await idle();
  assert.match(await page.locator('#theme-error').textContent(),/Please try again/);
  assert.equal((await select.inputValue()==='space'),true,'Failed save retains the draft');await currentTheme('quirq');
  assert.deepEqual(saved,{theme:'quirq'});await expectBranding();
  saveError=null;await save.click();await currentTheme('space');await idle();
  assert.equal((await select.inputValue()==='space'),true);await assertStyles('space');
  await page.reload({waitUntil:'networkidle'});await card.waitFor();await idle();await currentTheme('space');
  assert.equal((await select.inputValue()==='space'),true);await expectBranding();
  checked('A failed save preserves Neon and the Grove draft; retry and reload restore the original green theme without changing the name or logo.');

  readError='Theme settings are unavailable. Please try again.';
  await page.reload({waitUntil:'networkidle'});await card.waitFor();await idle();
  assert.equal(await select.isDisabled(),true);assert.equal(await save.isDisabled(),true);
  assert.equal(await page.locator('#theme-retry').isVisible(),true);
  assert.match(await page.locator('#theme-error').textContent(),/unavailable/);await expectBranding();
  const writesBeforeRetry=report.writes.length;
  readError=null;await page.locator('#theme-retry').click();await idle();
  assert.equal(await select.isEnabled(),true);assert.equal((await select.inputValue()==='space'),true);
  assert.equal(report.writes.length,writesBeforeRetry);assert.equal(await page.locator('#theme-error').isVisible(),false);
  checked('Unavailable reads disable theme writes, retain branding, and recover through Try again without a settings mutation.');

  await select.selectOption('quirq');await save.click();await currentTheme('quirq');await idle();await expectBranding();
  for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:1000});await card.scrollIntoViewIfNeeded();
    const layout=await page.evaluate(()=>{
      const rect=node=>{const r=node.getBoundingClientRect();return{left:r.left,right:r.right,width:r.width};};
      const card=document.querySelector('#setup-theme');
      return{viewport:innerWidth,scroll:document.documentElement.scrollWidth,card:rect(card),
        controls:[...card.querySelectorAll('select,button,label')].filter(node=>node.getClientRects().length).map(node=>({id:node.id||node.htmlFor,...rect(node)}))};
    });
    report.layouts.push(layout);assert.ok(layout.scroll<=width,'No horizontal overflow at '+width+'px');
    assert.ok(layout.card.left>=-1&&layout.card.right<=width+1,'Theme card fits at '+width+'px');
    for(const control of layout.controls)assert.ok(control.left>=-1&&control.right<=width+1,control.id+' fits at '+width+'px');
    await screenshot('theme-quirq-'+width+'.png');
  }
  checked('Neon theme controls and previews fit desktop, 390px and 320px layouts.');

  await page.setViewportSize({width:1440,height:1000});await page.locator('#tab-projects').click();
  await page.waitForURL('**/#/projects/overview');
  await page.locator('#view-graph.is-active').waitFor();await page.waitForLoadState('networkidle');
  await currentTheme('quirq');await assertStyles('quirq');await screenshot('theme-quirq-overview-1440.png',false);
  await page.goto(origin+'/space/#/projects/data/graph',{waitUntil:'networkidle'});
  await page.locator('#view-graph.is-active').waitFor();await page.waitForFunction(()=>document.querySelector('#gcanvas')?.width>0);
  await currentTheme('quirq');await screenshot('theme-quirq-graph-1440.png',false);
  assert.ok(report.requests.some(r=>r.path==='/xo/space.json'),'Graph loads fixture data');
  checked('Neon persists across Projects Overview and Graph, with screenshots for non-Setup theme coverage.');

  const legendColors=()=>page.locator('#legend .sw[style]').evaluateAll(nodes=>nodes.map(node=>getComputedStyle(node).backgroundColor));
  const quirqLegend=await legendColors();assert.ok(quirqLegend.length>0,'Graph has category swatches');
  await page.locator('#tab-setup').click();await card.waitFor();await idle();
  await select.selectOption('space');await save.click();await currentTheme('space');await idle();
  await page.goto(origin+'/space/#/projects/data/graph');
  await page.locator('#view-graph.is-active').waitFor();
  assert.notDeepEqual(await legendColors(),quirqLegend,'Existing graph adopts Grove category colors');
  await screenshot('theme-space-graph-1440.png',false);
  await page.locator('#tab-setup').click();await card.waitFor();await idle();
  await select.selectOption('quirq');await save.click();await currentTheme('quirq');await idle();
  await page.goto(origin+'/space/#/projects/data/graph');
  await page.locator('#view-graph.is-active').waitFor();
  assert.deepEqual(await legendColors(),quirqLegend,'Existing graph restores Neon category colors');
  checked('An already mounted Graph refreshes its legend through Neon → Grove → Neon without reloading the document.');
  await page.locator('#tab-setup').click();await card.waitFor();await idle();
  await select.selectOption('midnight');
  await currentTheme('quirq');
  assert.match(await page.locator('#theme-description').textContent(),/Periwinkle/);
  await save.click();await currentTheme('midnight');await idle();await expectBranding();
  await page.reload({waitUntil:'networkidle'});await card.waitFor();await idle();
  assert.equal(await select.inputValue(),'midnight');await currentTheme('midnight');
  const midnightStyle=await page.evaluate(()=>({bg:getComputedStyle(document.body).backgroundColor,accent:getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()}));
  assert.deepEqual(midnightStyle,{bg:'rgb(9, 12, 18)',accent:'#91adff'});
  for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:1000});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await screenshot('theme-midnight-'+width+'.png');
  }
  await page.setViewportSize({width:1440,height:1000});
  await page.goto(origin+'/space/#/projects/data/graph');await page.locator('#view-graph.is-active').waitFor();
  await page.locator('#legend .sw[style]').first().waitFor();
  assert.equal((await legendColors())[0],'rgb(145, 173, 255)');
  await screenshot('theme-midnight-graph.png',false);
  checked('Midnight saves, survives reload, preserves branding and applies its blue palette to the graph and responsive dropdown.');
  for(const theme of ['graphite','linen']){
    await page.goto(origin+'/space/#/setup/workspace',{waitUntil:'networkidle'});await card.waitFor();await idle();
    await select.selectOption(theme);await save.click();await currentTheme(theme);await idle();
    await page.reload({waitUntil:'networkidle'});await card.waitFor();await idle();
    assert.equal(await select.inputValue(),theme);await currentTheme(theme);await expectBranding();
    const appearance=await page.evaluate(()=>{
      const css=getComputedStyle(document.documentElement),body=getComputedStyle(document.body);
      const rgb=value=>value.match(/[\d.]+/g).slice(0,3).map(Number);
      const luminance=value=>rgb(value).map(c=>{c/=255;return c<=.04045?c/12.92:((c+.055)/1.055)**2.4;}).reduce((sum,c,i)=>sum+c*[.2126,.7152,.0722][i],0);
      const contrast=(a,b)=>{a=luminance(a);b=luminance(b);return(Math.max(a,b)+.05)/(Math.min(a,b)+.05);};
      const button=getComputedStyle(document.querySelector('.tabs .is-on'));
      return{scheme:css.colorScheme,bg:body.backgroundColor,textContrast:contrast(body.color,body.backgroundColor),buttonContrast:contrast(button.color,button.backgroundColor)};
    });
    assert.equal(appearance.scheme,theme==='linen'?'light':'dark');
    assert.ok(appearance.textContrast>=4.5,'Readable '+theme+' body text');assert.ok(appearance.buttonContrast>=4.5,'Readable '+theme+' active controls');
    for(const width of [1440,320]){
      await page.setViewportSize({width,height:1000});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      await screenshot('theme-'+theme+'-'+width+'.png');
    }
    await page.setViewportSize({width:1440,height:1000});
    await page.goto(origin+'/space/#/projects/data/graph');await page.locator('#legend .sw[style]').first().waitFor();
    assert.equal((await legendColors())[0],theme==='linen'?'rgb(166, 77, 47)':'rgb(238, 238, 238)');
    await screenshot('theme-'+theme+'-graph.png',false);
    await page.goto(origin+'/space/#/agents/overview',{waitUntil:'networkidle'});
    await page.locator('[data-slot="chart"]').first().waitFor();
    await screenshot('theme-'+theme+'-agents.png',false);
  }
  checked('Graphite and Linen persist, keep branding, meet text/button contrast, fit mobile, and theme graph and agent charts.');
  await page.goto(origin+'/space/#/setup/workspace',{waitUntil:'networkidle'});await card.waitFor();await idle();
  await select.selectOption('space');
  const writesBeforeRefresh=report.writes.length;
  await Promise.all([page.waitForNavigation({waitUntil:'networkidle'}),page.locator('#space-refresh').click()]);
  await card.waitFor();await idle();await currentTheme('linen');
  assert.equal(await select.inputValue(),'linen','Global refresh restores the saved theme rather than the draft');
  assert.equal(report.writes.length,writesBeforeRefresh,'Global refresh does not save a draft');
  checked('The global full-page Refresh restores the saved theme and preserves branding.');
  assert.deepEqual(report.errors,[],'No unexpected browser, console or HTTP errors');
  console.log(JSON.stringify(report,null,2));
}catch(error){
  report.failure=error.stack;
  await page.screenshot({path:resolve(output,'failure.png')}).catch(()=>{});
  throw error;
}finally{
  for(const pending of pendingGates)pending.release.resolve();
  await writeFile(resolve(output,'report.json'),JSON.stringify(report,null,2));
  await browser.close();
}
