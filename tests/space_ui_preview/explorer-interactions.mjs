#!/usr/bin/env node
/* Explorer interaction regression over fictional data. Delayed GETs exercise
   stale snapshots. One secret write is fulfilled in browser memory; every
   other mutation and all external requests are blocked. */
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5100';
const endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');
assert.ok(!['5002','5112'].includes(endpoint.port),'Use the fictional preview server');
const output=resolve(process.argv[2]||'/tmp/space-explorer-interactions');
await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE
  ?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const browser=await chromium.launch({headless:true,
  executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
const page=await context.newPage();page.setDefaultTimeout(15000);
const report={checks:[],fits:[],errors:[],blocked:[],mockWrites:[],sessionReads:0,secretReads:0};
page.on('pageerror',error=>report.errors.push(error.message));
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
const gate=()=>({arrived:deferred(),release:deferred()});
const pending=new Set();let holdRead=null,version=0;
const secretItems=[{key:'FIXTURE_ORIGINAL',is_set:true}];
function sessions(){
  const rows=Array.from({length:40},(_,index)=>({id:'needle-'+String(index).padStart(2,'0'),agent:'demo',
    key:'demo:needle-'+index,project:'Fixture project',project_path:'/fixture/project',
    model:'fixture-model',started_at:'2026-09-14T09:00:00Z',total_tokens:index,subagents:[],tools:[]}));
  if(version)rows.push({id:'freshly-loaded-record',agent:'demo',project:'Fixture project',
    project_path:'/fixture/project',subagents:[],tools:[]});
  if(version>1)rows.push({id:'queued-fresh-record',agent:'demo',project:'Fixture project',
    project_path:'/fixture/project',subagents:[],tools:[]});
  return{meta:{sources:[{id:'demo',label:'Demo runtime',available:true}]},
    totals:{sessions:rows.length,sessions_by_agent:{demo:rows.length}},sessions:rows};
}
const json=(route,data)=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url());
  if(url.origin===endpoint.origin&&url.pathname==='/api/secrets/FIXTURE_ADDED'&&request.method()==='PATCH'){
    assert.deepEqual(request.postDataJSON(),{value:'fictional-value-never-stored'});
    secretItems.push({key:'FIXTURE_ADDED',is_set:true});report.mockWrites.push('PATCH '+url.pathname);
    return json(route,{ok:true,key:'FIXTURE_ADDED',is_set:true});
  }
  if(url.origin!==endpoint.origin||request.method()!=='GET'){
    report.blocked.push(request.method()+' '+request.url());return route.abort();
  }
  if(url.pathname==='/xo/sessions.json'){
    report.sessionReads++;
    const snapshot=sessions(),wait=holdRead;holdRead=null;
    if(wait){pending.add(wait);wait.arrived.resolve();await wait.release.promise;pending.delete(wait);}
    return json(route,snapshot);
  }
  if(url.pathname==='/api/inbox')return json(route,{counts:{new:0,seen:0,done:0},items:[]});
  if(url.pathname==='/api/secrets'){report.secretReads++;return json(route,{items:secretItems});}
  if(url.pathname==='/api/schedules')return json(route,{jobs:[]});
  if(url.pathname==='/api/connections')return json(route,{signed_in:false,poller_enabled:false,connections:[]});
  if(url.pathname==='/space/server/status')return json(route,{status:'on',running:true,instance_id:'fixture'});
  return route.continue();
});
const active=page.locator('#view-agents-explorer');
const action=name=>active.locator('[data-explorer-action="'+name+'"]');
const warning=active.locator('.space-explorer-warning');
const checked=message=>{report.checks.push(message);console.log(message);};
async function focused(control,message){assert.equal(await control.evaluate(node=>node===document.activeElement),true,message);}
async function mode(name){
  await page.locator('#section-nav [data-view-mode="'+name+'"]').click();
  await page.waitForURL('**/#/agents/'+name);
  await page.waitForFunction(name=>document.querySelector('#section-nav [data-view-mode="'+name+'"]')?.getAttribute('aria-current')==='page',name);
}
async function refreshDone(){
  await page.waitForFunction(()=>!document.querySelector('#view-agents-explorer [data-explorer-action="refresh"]')?.disabled);
}
async function awaitRead(wait){
  let timer;
  try{await Promise.race([wait.arrived.promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Expected session GET did not arrive')),15000);})]);}
  finally{clearTimeout(timer);}
}

try{
  await page.goto(origin+'/space/#/agents/graph',{waitUntil:'networkidle'});
  await active.locator('g[data-record]').first().waitFor();await refreshDone();
  const zoom=action('in'),originalZoom=await zoom.elementHandle();
  await zoom.focus();
  let width=await active.locator('svg').evaluate(svg=>svg.width.baseVal.value);
  for(let index=0;index<2;index++){
    await page.keyboard.press('Enter');await focused(zoom,'Repeated keyboard zoom retains focus');
    const next=await active.locator('svg').evaluate(svg=>svg.width.baseVal.value);
    assert.ok(next>width,'Each keyboard activation zooms again');width=next;
    assert.equal(await originalZoom.evaluate(node=>node===document.querySelector('#view-agents-explorer [data-explorer-action="in"]')),true);
  }
  await active.locator('g[data-record="agents"]').click();
  await action('group').focus();
  const before=await active.locator('g[data-record]').count();
  await page.keyboard.press('Enter');await focused(action('group'),'Collapsed graph group restores its action focus');
  assert.ok(await active.locator('g[data-record]').count()<before);
  await page.keyboard.press('Enter');await focused(action('group'),'Expanded graph group supports repeated keyboard activation');
  assert.equal(await active.locator('g[data-record]').count(),before);
  checked('Keyboard zoom and graph group controls retain focus and respond to repeated activation.');

  await mode('tree');await active.locator('.space-tree-node').first().waitFor();
  const expandedCount=await active.locator('.space-tree-node').count();
  await action('collapse').focus();
  for(let index=0;index<2;index++){await page.keyboard.press('Enter');await focused(action('collapse'),'Collapse all retains keyboard focus');}
  assert.ok(await active.locator('.space-tree-node').count()<expandedCount);
  await action('expand').focus();
  for(let index=0;index<2;index++){await page.keyboard.press('Enter');await focused(action('expand'),'Expand all retains keyboard focus');}
  assert.equal(await active.locator('.space-tree-node').count(),expandedCount);
  await page.locator('#view-search').fill('needle-01');
  assert.equal(await action('collapse').isDisabled(),true);
  assert.equal(await action('expand').isDisabled(),true);
  assert.ok(await active.locator('[data-toggle-record]').count()>0);
  assert.equal(await active.locator('[data-toggle-record]').evaluateAll(nodes=>nodes.every(node=>node.disabled)),true);
  assert.equal(await active.locator('.space-tree-node').count(),4,'Search retains the matching session and its actual ancestors');
  await page.locator('#view-search').fill('');
  assert.equal(await action('collapse').isEnabled(),true);
  assert.equal(await action('expand').isEnabled(),true);
  checked('Tree keyboard controls remain usable; search disables ineffective collapse controls and clearing search restores them.');

  await mode('graph');await page.setViewportSize({width:320,height:900});
  for(const query of ['needle-01','needle']){
    await page.locator('#view-search').fill(query);
    await action('fit').click();
    const fit=await active.locator('.space-explorer-surface').evaluate(surface=>{
      const svg=surface.querySelector('svg'),bounds=svg.getBoundingClientRect(),box=surface.getBoundingClientRect();
      const style=getComputedStyle(surface.querySelector('.space-graph'));
      const horizontal=parseFloat(style.paddingLeft)+parseFloat(style.paddingRight);
      const vertical=parseFloat(style.paddingTop)+parseFloat(style.paddingBottom);
      return{width:surface.clientWidth,height:surface.clientHeight,scrollWidth:surface.scrollWidth,scrollHeight:surface.scrollHeight,
        svgWidth:bounds.width,svgHeight:bounds.height,horizontal,vertical,viewWidth:svg.viewBox.baseVal.width,viewHeight:svg.viewBox.baseVal.height,
        left:bounds.left-box.left,top:bounds.top-box.top,right:bounds.right-box.left,bottom:bounds.bottom-box.top};
    });
    assert.ok(fit.svgWidth+fit.horizontal<=fit.width+1,query+' graph fits available width including padding');
    assert.ok(fit.svgHeight+fit.vertical<=fit.height+1,query+' graph fits available height including padding');
    assert.ok(fit.scrollWidth<=fit.width+1&&fit.scrollHeight<=fit.height+1,query+' fitted graph needs no scrolling');
    if(query==='needle')assert.ok((fit.height-fit.vertical)/fit.viewHeight<(fit.width-fit.horizontal)/fit.viewWidth,'Tall fixture genuinely tests the height limit');
    report.fits.push({query,...fit});
  }
  checked('Fit handles both a wide searched path and a tall searched graph at 320px, including container padding.');

  await page.setViewportSize({width:1440,height:1000});await page.locator('#view-search').fill('');
  assert.equal(await warning.isVisible(),false);
  const stale=holdRead=gate();await action('refresh').click();await awaitRead(stale);
  version=1;
  await page.evaluate(()=>dispatchEvent(new CustomEvent('space:projects-changed',{detail:{project_id:'fictional-change'}})));
  assert.equal(await warning.isVisible(),true,'Change notice appears immediately while the request is pending');
  assert.match(await warning.textContent(),/Projects changed/);
  stale.release.resolve();await refreshDone();
  assert.match(await warning.textContent(),/Projects changed/,'An older response cannot clear the change notice');
  await mode('tree');
  assert.equal(await active.locator('[data-record]').filter({hasText:'freshly-loaded-record'}).count(),0,'The delayed response really contains the old snapshot');
  assert.match(await warning.textContent(),/Projects changed/,'Changing representation preserves the notice');
  const fresh=holdRead=gate();await action('refresh').click();await awaitRead(fresh);
  assert.equal(await warning.isVisible(),true,'Starting a fresh request does not claim the data is already current');
  fresh.release.resolve();await refreshDone();
  await active.locator('[data-record]').filter({hasText:'freshly-loaded-record'}).waitFor();
  assert.equal(await warning.isVisible(),false,'Only a completed fresh snapshot clears the change notice');
  assert.equal(report.sessionReads,3,'One initial snapshot, one stale refresh and one fresh refresh');
  checked('Project changes during a delayed refresh remain visible until a later fresh response arrives.');

  await mode('graph');await active.locator('g[data-record="agents"]').click();
  assert.match(await action('group').textContent(),/Collapse group/);await action('group').click();
  assert.equal(await active.locator('g[data-record]').count(),1);
  const reentry=holdRead=gate();await action('refresh').click();await awaitRead(reentry);
  await page.locator('#wiki-link').click();await page.waitForURL('**/#/wiki');
  await page.locator('#view-wiki.is-active h1').waitFor();
  version=2;
  await page.evaluate(()=>{location.hash='#/agents/graph';});await page.waitForURL('**/#/agents/graph');
  await page.locator('#view-agents-explorer.is-active').waitFor();
  await page.waitForFunction(()=>document.querySelector('#section-nav [data-view-mode="graph"]')?.getAttribute('aria-current')==='page');
  await active.locator('.space-explorer-count').filter({hasText:'Refreshing'}).waitFor();
  assert.equal(report.sessionReads,4,'Re-entry queues behind the pending request instead of starting a parallel one');
  const queued=holdRead=gate();reentry.release.resolve();await awaitRead(queued);
  assert.equal(await action('refresh').isDisabled(),true,'Refresh stays busy throughout the queued read');
  assert.match(await active.locator('.space-explorer-count').textContent(),/Refreshing/);
  queued.release.resolve();await refreshDone();
  assert.equal(await active.locator('g[data-record]').count(),1,'A deliberately collapsed root stays collapsed after re-entry refresh');
  await mode('tree');await active.locator('[data-record]').filter({hasText:'queued-fresh-record'}).waitFor();
  const reads=report.sessionReads;await mode('graph');await mode('tree');
  assert.equal(report.sessionReads,reads,'Graph and Tree changes reuse the same fresh snapshot');
  assert.equal(reads,5,'Physical re-entry triggers exactly one queued fresh snapshot');
  checked('Physical section re-entry queues a fresh read behind a pending one; Graph and Tree switches stay cached.');

  await page.evaluate(()=>{location.hash='#/setup/graph';});await page.waitForURL('**/#/setup/graph');
  const setup=page.locator('#view-setup-explorer');
  await setup.locator('[data-record]').first().waitFor();
  await page.waitForFunction(()=>!document.querySelector('#view-setup-explorer [data-explorer-action="refresh"]')?.disabled);
  await page.locator('#view-search').fill('FIXTURE');
  await setup.locator('[data-record]').filter({hasText:'FIXTURE_ORIGINAL'}).click();
  const selection=await setup.locator('[data-record].is-selected').getAttribute('data-record');
  await setup.locator('[data-explorer-action="in"]').click();
  const originalScale=await setup.locator('svg').evaluate(svg=>svg.width.baseVal.value/svg.viewBox.baseVal.width);
  await page.locator('#section-nav [data-section-page="setup/secrets"]').click();
  await page.waitForURL('**/#/setup/secrets');await page.locator('#secret-list').filter({hasText:'FIXTURE_ORIGINAL'}).waitFor();
  await page.locator('#secret-add').click();await page.locator('#secret-key').fill('FIXTURE_ADDED');
  await page.locator('#secret-value').fill('fictional-value-never-stored');await page.locator('#secret-save').click();
  await page.locator('#secret-list').filter({hasText:'FIXTURE_ADDED'}).waitFor();
  await page.waitForFunction(()=>!document.querySelector('#setup-refresh').disabled);
  const beforeReturn=report.secretReads;
  await page.locator('#section-nav [data-view-mode="graph"]').click();await page.waitForURL('**/#/setup/graph');
  await setup.locator('[data-record]').filter({hasText:'FIXTURE_ADDED'}).waitFor();
  assert.equal(report.secretReads,beforeReturn+1,'List edit is reflected by automatic re-entry refresh');
  assert.equal(await page.locator('#view-search').inputValue(),'FIXTURE','Re-entry keeps the explorer query');
  assert.equal(await setup.locator('[data-record].is-selected').getAttribute('data-record'),selection,'Re-entry keeps selection');
  assert.equal(await setup.locator('svg').evaluate(svg=>svg.width.baseVal.value/svg.viewBox.baseVal.width),originalScale,'Re-entry keeps graph zoom');
  const setupReads=report.secretReads;
  for(const name of ['tree','graph']){
    await page.locator('#section-nav [data-view-mode="'+name+'"]').click();await page.waitForURL('**/#/setup/'+name);
    await setup.locator('[data-record]').filter({hasText:'FIXTURE_ADDED'}).waitFor();
  }
  assert.equal(report.secretReads,setupReads,'Setup Graph and Tree reuse the updated snapshot');
  assert.doesNotMatch(await setup.textContent(),/fictional-value-never-stored/,'Explorer shows saved names, never secret values');
  assert.deepEqual(report.mockWrites,['PATCH /api/secrets/FIXTURE_ADDED']);
  checked('A secret saved in List appears on Graph re-entry without Refresh, preserving query, selection and zoom.');
  assert.deepEqual(report.errors,[]);assert.deepEqual(report.blocked,[]);
}catch(error){
  report.failure=error.stack;
  await page.screenshot({path:resolve(output,'failure.png'),animations:'disabled'}).catch(()=>{});
  throw error;
}finally{
  for(const wait of pending)wait.release.resolve();
  await writeFile(resolve(output,'report.json'),JSON.stringify(report,null,2));
  await browser.close();
}
