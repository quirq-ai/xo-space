#!/usr/bin/env node
/* One page-level refresh, exercised against actual Space assets and fictional
   GET fixtures. External requests and all service mutations are blocked. */
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5100';
const endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');
assert.ok(!['5002','5112'].includes(endpoint.port),'Use the read-only fictional preview server');
const output=resolve(process.argv[2]||'/tmp/space-global-refresh');
await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE
  ?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const browser=await chromium.launch({headless:true,
  executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000},
  deviceScaleFactor:1,locale:'en-US',timezoneId:'UTC',reducedMotion:'reduce'});
const page=await context.newPage();
page.setDefaultTimeout(15000);
const report={origin,fixture:'Fictional read-only preview with synthetic Inbox, Jobs and Quirq data',
  checks:[],layouts:[],screenshots:[],errors:[],blocked:[],documents:0};
let inboxTitle='Review the Aurora release';
const json=(route,data)=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url());
  if(url.origin!==endpoint.origin||request.method()!=='GET'){
    report.blocked.push(request.method()+' '+request.url());
    return route.abort();
  }
  if(request.isNavigationRequest()&&request.resourceType()==='document')report.documents++;
  if(url.pathname==='/api/inbox')return json(route,{items:[{id:'release',title:inboxTitle,
    kind:'note',source:'agent',status:'seen',ts:'2026-09-14T10:00:00Z',
    body:'A fictional release handoff for browser review.'}],counts:{new:0,seen:1,done:0,open:1},total:1});
  if(url.pathname==='/api/schedules')return json(route,{jobs:[]});
  if(url.pathname==='/api/telemetry/sources')return json(route,{items:[]});
  if(url.pathname==='/api/connectors/composio/backend')return json(route,{mode:'inactive',key_source:null});
  if(url.pathname==='/api/quirq')return json(route,{root:{host_path:'/demo/.quirq',readable:true,writable:true},
    totals:{files:0,bytes:0},watcher:{enabled:false},activity:{},tree:[],project_outputs:{project_count:10}});
  return route.continue();
});
page.on('pageerror',error=>report.errors.push(error.message));
page.on('console',message=>{if(message.type()==='error')report.errors.push(message.text());});
page.on('response',response=>{if(response.status()>=400)report.errors.push(response.status()+' '+response.url());});

const routes=[
  ...['overview','data/list','data/graph','data/tree','timeline','manage'].map(slug=>'projects/'+slug),
  ...['overview','sessions','trends','configure'].map(slug=>'agents/'+slug),
  ...['items','connections','jobs','activity','sharing-activity','sharing'].map(slug=>'inbox/'+slug),
  ...['workspace','intelligence','connectors','secrets','commands','server','server/details'].map(slug=>'setup/'+slug),
  'wiki',
];
const refresh=page.locator('.topbar #space-refresh');
const checked=text=>{report.checks.push(text);console.log(text);};
async function go(route){
  await page.evaluate(route=>{location.hash='#/'+route;},route);
  await page.waitForFunction(route=>location.hash==='#/'+route
    &&document.querySelectorAll('.view.is-active').length===1,route);
  await page.waitForLoadState('networkidle');
}
async function oneRefresh(route){
  assert.equal(await refresh.isVisible(),true,route+' has a visible global Refresh');
  assert.equal(await refresh.isEnabled(),true,route+' global Refresh is enabled');
  assert.equal(await page.getByRole('button',{name:/refresh/i}).count(),1,
    route+' exposes exactly one Refresh button');
  assert.equal(await refresh.getAttribute('type'),'button');
  assert.match(await refresh.getAttribute('aria-label')||await refresh.textContent(),/refresh/i);
}
async function reload({keyboard=false,palette=false}={}){
  const before=page.url(),documents=report.documents;
  await page.evaluate(()=>{window.fixtureRefreshSentinel='old document';});
  if(palette){
    await page.keyboard.press('Meta+k');
    await page.getByRole('combobox',{name:'Command menu search'}).fill('Refresh this page');
    await page.getByRole('option',{name:'Refresh this page',exact:true}).waitFor();
  }
  if(keyboard)await refresh.focus();
  await Promise.all([
    page.waitForNavigation({waitUntil:'networkidle'}),
    palette?page.getByRole('option',{name:'Refresh this page',exact:true}).click()
      :keyboard?refresh.press('Enter'):refresh.click(),
  ]);
  assert.equal(page.url(),before,'Full reload retains the current path, query and hash');
  assert.equal(report.documents,documents+1,'One activation requests one new document');
  assert.equal(await page.evaluate(()=>window.fixtureRefreshSentinel),undefined,'Document state is replaced');
  assert.equal(await page.evaluate(()=>performance.getEntriesByType('navigation')[0].type),'reload');
  await oneRefresh(new URL(before).hash);
}
async function layout(route,width){
  await page.evaluate(()=>new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done))));
  const bounds=await page.evaluate(()=>{
    const box=node=>{const rect=node.getBoundingClientRect();return{
      x:rect.x,y:rect.y,right:rect.right,bottom:rect.bottom,width:rect.width,height:rect.height};};
    const header=document.querySelector('.topbar');
    const visible=node=>node.getClientRects().length&&getComputedStyle(node).visibility!=='hidden';
    return{viewport:innerWidth,scroll:document.documentElement.scrollWidth,header:box(header),
      stage:box(document.querySelector('#stage')),refresh:box(document.querySelector('#space-refresh')),
      controls:[...header.querySelectorAll('.brand .mark,.brand b,.tabs a,.resource-links a,.resource-links button,#view-search-wrap')]
        .filter(visible).map(node=>({name:node.id||node.textContent.trim(),...box(node)}))};
  });
  assert.ok(bounds.scroll<=width,route+' does not overflow at '+width+'px');
  assert.ok(bounds.refresh.width>=36&&bounds.refresh.height>=36,'Refresh has a usable hit area');
  for(const control of bounds.controls){
    assert.ok(control.x>=-1&&control.right<=width+1,route+' '+control.name+' fits the viewport at '+width+'px');
    assert.ok(control.y>=bounds.header.y-1&&control.bottom<=bounds.header.bottom+1,
      route+' '+control.name+' fits the top bar at '+width+'px');
  }
  for(const [index,a] of bounds.controls.entries())for(const b of bounds.controls.slice(index+1)){
    assert.equal(Math.min(a.right,b.right)-Math.max(a.x,b.x)>1
      &&Math.min(a.bottom,b.bottom)-Math.max(a.y,b.y)>1,false,
    route+' '+a.name+' overlaps '+b.name+' at '+width+'px');
  }
  assert.ok(bounds.stage.y>=bounds.header.bottom-1,route+' content clears the top bar at '+width+'px');
  report.layouts.push({route,width,...bounds});
}
async function screenshot(name){
  await page.mouse.move(1,999);
  await page.screenshot({path:resolve(output,name),animations:'disabled'});
  report.screenshots.push(name);
}

try{
  await page.goto(origin+'/space/?review=global-refresh#/projects/overview',{waitUntil:'networkidle'});
  for(const route of routes){await go(route);await oneRefresh(route);}
  await go('projects/manage');
  await page.locator('.manage-project-toggle').first().click();
  await page.locator('[data-iss-refresh]').first().waitFor();
  assert.equal(await page.locator('[data-iss-refresh]').first().textContent(),'Check GitHub');
  await oneRefresh('Projects Manage with Issues open');
  checked('All 24 pages expose one global Refresh; expanded Issues retains its distinct Check GitHub action.');

  for(const route of ['projects/data/list','agents/sessions','inbox/items','setup/connectors','wiki']){
    await go(route);await reload({keyboard:route==='setup/connectors'});
  }
  checked('Projects, Agents, Inbox, Setup and Wiki reload the document and preserve path, query and hash; keyboard Enter works.');
  await go('projects/data/list');
  await reload({palette:true});
  checked('Command K opens the palette, and Refresh this page reloads the document while preserving the current URL.');
  await go('inbox/items');
  await page.getByText(inboxTitle,{exact:true}).waitFor();
  inboxTitle='Updated Aurora release handoff';
  await reload();
  await page.getByText(inboxTitle,{exact:true}).waitFor();
  await reload();
  await page.getByText(inboxTitle,{exact:true}).waitFor();
  checked('Refresh fetches changed Inbox data, and a later refresh works again.');

  for(const width of [1440,1600,1601,1920,390,320]){
    await page.setViewportSize({width,height:1000});
    for(const route of routes){await go(route);await oneRefresh(route);await layout(route,width);}
    for(const route of [1440,390,320].includes(width)?['projects/overview','projects/manage']:[]){
      await go(route);
      await page.locator('.view.is-active').evaluate(node=>{node.scrollTop=0;});
      await screenshot(route.replaceAll('/','-')+'-'+width+'.png');
    }
    checked(width+'px: every page fits without header overlap or document overflow.');
  }
  assert.deepEqual(report.blocked,[],'No external requests or service writes were attempted');
  assert.deepEqual(report.errors,[],'No browser errors or unsuccessful responses');
}catch(error){report.failure=error.stack;await screenshot('failure.png').catch(()=>{});throw error;}
finally{await writeFile(resolve(output,'report.json'),JSON.stringify(report,null,2));await browser.close();}
