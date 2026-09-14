#!/usr/bin/env node
/* Real shared List/Graph/Tree controls over fictional endpoint responses.
   Every external request and service mutation is blocked. */
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5100';
const endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');assert.ok(!['5002','5112'].includes(endpoint.port));
const output=resolve(process.argv[2]||'/tmp/space-data-views');await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000},deviceScaleFactor:1,locale:'en-US',timezoneId:'UTC',reducedMotion:'reduce'});
const page=await context.newPage();page.setDefaultTimeout(15000);
const report={checks:[],screenshots:[],layouts:[],errors:[],writes:[]};
page.on('pageerror',error=>report.errors.push(error.message));
const stamp='2026-09-14T09:00:00Z';
const json=(route,data)=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
await context.route('**/*',route=>{
  const request=route.request(),url=new URL(request.url());
  if(url.origin!==endpoint.origin||request.method()!=='GET'){
    report.writes.push(request.method()+' '+request.url());return route.abort();
  }
  if(/^\/api\/connectors\/(github|vercel)\/status$/.test(url.pathname))return json(route,{status:'needs_auth'});
  if(url.pathname==='/api/connectors/magicpath/status')return json(route,{cli_installed:false,skill_installed:false,logged_in:false});
  if(/^\/api\/connectors\/(gdrive|onedrive)\/remotes$/.test(url.pathname))return json(route,{remotes:[]});
  if(url.pathname==='/api/connectors/composio/toolkits')return json(route,{toolkits:[]});
  if(url.pathname==='/api/connections')return json(route,{signed_in:true,poller_enabled:true,connections:[{
    toolkit:'mail-demo',display_name:'Demo mail',configured:true,connected_here:true,enabled:true,interval_s:900,
    collectors:['messages'],available_collectors:[{id:'messages',label:'Recent messages'}],account_label:'fixture@example.com',last_poll_at:stamp}]});
  if(url.pathname==='/api/inbox')return json(route,{counts:{new:0,seen:2,done:0},items:[
    {id:'review',title:'Review Aurora summary',status:'seen',source:'issues',kind:'issue',project_id:'aurora-console',ts:stamp},
    {id:'event',title:'Workspace event',status:'seen',source:'timeline',kind:'event',ts:stamp}]});
  if(url.pathname==='/api/schedules')return json(route,{jobs:[{id:'daily-sync',name:'Daily sync',every_seconds:3600,enabled:true,running:false,command:{argv:['echo','fixture']},
    last_result:{status:'ok',finished_at:stamp,duration_seconds:2}}]});
  if(url.pathname==='/xo/sessions.json')return json(route,{meta:{sources:[{id:'demo',label:'Demo runtime',available:true}]},totals:{sessions:1,sessions_by_agent:{demo:1}},
    sessions:[{id:'aurora-session',agent:'demo',project:'Aurora Console',project_path:'/fixture/aurora-console',model:'demo-model',started_at:stamp,
      ended_at:stamp,duration_sec:60,total_tokens:100,turns:2,cost_known:false,subagents:[],tools:[]}],daily_sessions:[],daily_models:[],daily_tools:[]});
  return route.continue();
});
const defaults={projects:'projects/list',agents:'agents/sessions',inbox:'inbox/items',setup:'setup/workspace'};
const checked=message=>{report.checks.push(message);console.log(message);};
const activeMode=()=>page.locator('#section-nav [data-view-mode][aria-current="page"]');
function sectionFor(route){
  const [domain,leaf]=route.split('/');
  if(domain==='projects')return ({list:'projects',overview:'graph',graph:'graph',tree:'tree',sharing:'sharing',timeline:'time'})[leaf];
  if(['graph','tree'].includes(leaf))return domain+'-explorer';
  return domain;
}
async function ready(route){
  await page.waitForURL('**/#/'+route);
  await page.waitForFunction(section=>document.querySelector('#view-'+section)?.classList.contains('is-active'),sectionFor(route));
  await page.locator('.view.is-active h1:visible').first().waitFor();
}
async function routeTo(route){
  await page.evaluate(route=>{location.hash='#/'+route;},route);await ready(route);
}
async function mode(name,domain){
  const link=page.locator('#section-nav [data-view-mode="'+name+'"]');
  const target=(await link.getAttribute('href')).slice(2);await link.click();await ready(target);
  if(name!=='list')await page.waitForURL('**/#/'+domain+'/'+name);
  await page.locator('#section-nav [data-view-mode="'+name+'"][aria-current="page"]').waitFor();
  if(domain!=='projects'&&name!=='list')await page.locator('.view.is-active [data-record]').first().waitFor();
}
async function inspect(domain,label){
  const selector=page.locator('.view.is-active [data-record]').filter({hasText:label});
  await selector.first().click();
  await page.waitForFunction(label=>document.querySelector('.view.is-active .space-explorer-inspector h2')?.textContent===label,label);
}
async function layout(label){
  const bounds=await page.evaluate(()=>{
    const view=document.querySelector('.view.is-active').getBoundingClientRect(),nav=document.querySelector('#section-nav').getBoundingClientRect();
    const modes=[...document.querySelectorAll('[data-view-mode]')].map(link=>{const r=link.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom};});
    return {width:innerWidth,scroll:document.documentElement.scrollWidth,viewTop:view.top,navBottom:nav.bottom,modes};
  });
  assert(bounds.scroll<=bounds.width,label+' document fits');
  assert(bounds.viewTop>=bounds.navBottom-1,label+' content clears navigation');
  assert(bounds.modes.every(link=>link.left>=0&&link.right<=bounds.width+1),label+' all three mode controls fit');
  assert.equal(await page.locator('.view.is-active h1:visible').count(),1,label+' has one visible page heading');
  report.layouts.push({label,...bounds});
}
async function shot(name){await page.screenshot({path:resolve(output,name),animations:'disabled'});report.screenshots.push(name);}

try{
  await page.goto(origin+'/space/',{waitUntil:'networkidle'});
  await page.waitForURL('**/#/projects/list');
  await page.evaluate(()=>{window.dataViewsDocument='same-document';});
  for(const [domain,route] of Object.entries(defaults)){
    await page.locator('#tab-'+domain).click();await page.waitForURL('**/#/'+route);
    assert.deepEqual(await page.locator('#section-nav [data-view-mode]').evaluateAll(links=>links.map(link=>[link.tagName,link.dataset.viewMode])),[['A','list'],['A','graph'],['A','tree']]);
    assert.equal(await activeMode().getAttribute('data-view-mode'),'list');
    for(const name of ['graph','tree','list']){
      await mode(name,domain);
      if(name==='list')await page.waitForURL('**/#/'+route);
    }
  }
  checked('All four primary domains default to List and expose the same native List, Graph and Tree controls.');
  for(const [domain,label,query,ancestor] of [['agents','aurora-session','aurora-session','Demo runtime'],
    ['inbox','Review Aurora summary','Review Aurora summary','issues'],['setup','Workspace','Workspace','Setup']]){
    await routeTo(domain+'/tree');await page.locator('.view.is-active .space-tree-node').first().waitFor();
    const search=page.locator('#view-search');await search.fill(query);
    await inspect(domain,label);
    const selected=await page.locator('.view.is-active [data-record].is-selected').getAttribute('data-record');
    assert(await page.locator('.view.is-active [data-record]').filter({hasText:ancestor}).count()>0,'Matching records retain '+ancestor+' ancestor');
    await mode('graph',domain);assert.equal(await search.inputValue(),query);
    assert.equal(await page.locator('.view.is-active [data-record].is-selected').getAttribute('data-record'),selected);
    assert.equal(await page.locator('.view.is-active .space-explorer-inspector h2').textContent(),label);
    await page.locator('.view.is-active [data-record].is-selected').focus();await page.keyboard.press('Enter');
    await mode('tree',domain);assert.equal(await search.inputValue(),query);
    assert.equal(await page.locator('.view.is-active [data-record].is-selected').getAttribute('data-record'),selected);
    await search.fill('no-fixture-record-matches');await page.locator('.view.is-active .space-explorer-empty').waitFor();
    assert.match(await page.locator('.view.is-active .space-explorer-empty').textContent(),/No records match/);
    await search.fill('');await page.locator('.view.is-active [data-record]').first().waitFor();
  }
  checked('Graph and Tree retain search and selection; filters keep real ancestors, and graph records support keyboard inspection.');
  await routeTo('setup/secrets');await page.locator('#secret-add').click();
  await page.locator('#secret-key').fill('FICTIONAL_DRAFT');await page.locator('#secret-value').fill('fixture-only-unsaved');
  const field=await page.locator('#secret-value').elementHandle();
  await mode('graph','setup');await mode('tree','setup');await mode('list','setup');
  await page.waitForURL('**/#/setup/secrets');
  assert.equal(await page.locator('#secret-key').inputValue(),'FICTIONAL_DRAFT');
  assert.equal(await page.locator('#secret-value').inputValue(),'fixture-only-unsaved');
  assert.equal(await field.evaluate(node=>node===document.getElementById('secret-value')),true);
  await mode('tree','setup');await inspect('setup','Commands');
  await page.locator('.view.is-active .space-explorer-inspector a').click();await page.waitForURL('**/#/setup/commands');
  await page.locator('#setup-panel-commands').waitFor();
  await routeTo('setup/secrets');assert.equal(await page.locator('#secret-value').inputValue(),'fixture-only-unsaved');
  checked('List returns to the last Setup page with the same unsaved credential field; inspector links open existing controls without saving.');
  await routeTo('inbox/items');await mode('graph','inbox');await mode('tree','inbox');
  await page.goBack();await page.waitForURL('**/#/inbox/graph');await activeMode().filter({hasText:'Graph'}).waitFor();
  await page.goBack();await page.waitForURL('**/#/inbox/items');await activeMode().filter({hasText:'List'}).waitFor();
  await page.goForward();await page.waitForURL('**/#/inbox/graph');
  assert.equal(await page.evaluate(()=>window.dataViewsDocument),'same-document');
  checked('Browser history traverses modes without reloading or losing mounted List controllers.');
  for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:1000});
    for(const [domain,route] of Object.entries(defaults)){
      await routeTo(route);
      for(const name of ['list','graph','tree']){
        if(name!=='list')await mode(name,domain);
        await page.waitForLoadState('networkidle');
        await layout(domain+'-'+name+'-'+width);await shot(domain+'-'+name+'-'+width+'.png');
      }
    }
  }
  checked('All four domains and three modes fit desktop, 390px and 320px with one page heading and unobstructed controls.');
  assert.deepEqual(report.errors,[]);assert.deepEqual(report.writes,[]);
}catch(error){report.failure=error.stack;await shot('failure.png').catch(()=>{});throw error;}
finally{await writeFile(resolve(output,'report.json'),JSON.stringify(report,null,2));await browser.close();}
