#!/usr/bin/env node
/* Canonical section navigation over actual Space assets and fictional data.
   External requests and every service mutation are blocked. */
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {openProjectList,openProjectPage} from './routes.mjs';

const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5100';
const endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');
assert.ok(!['5002','5112'].includes(endpoint.port),'Use the fictional preview server');
const output=resolve(process.argv[2]||'/tmp/space-section-navigation');
await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE
  ?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000},deviceScaleFactor:1,
  locale:'en-US',timezoneId:'UTC',reducedMotion:'reduce'});
await context.addInitScript(()=>{Date.now=()=>Date.parse('2026-09-14T10:00:00Z');});
const page=await context.newPage();page.setDefaultTimeout(15000);
const report={fixture:'Actual assets with fictional read-only responses',checks:[],screenshots:[],layouts:[],errors:[],writes:[]};
page.on('pageerror',error=>report.errors.push(error.message));
page.on('console',message=>{if(message.type()==='error')report.errors.push(message.text());});
page.on('response',response=>{if(response.status()>=400)report.errors.push(response.status()+' '+response.url());});
const stamp='2026-09-14T09:00:00Z';
const sessions=Array.from({length:3},(_,index)=>({id:'fixture-'+index,agent:'demo',project:index?'Orbit API':'Aurora Console',
  project_path:index?'/demo/orbit-api':'/demo/aurora-console',model:'demo-model',started_at:stamp,ended_at:stamp,
  duration_sec:60,total_tokens:100,cost:0,cost_known:false,turns:1,subagents:[],tools:[]}));
const inbox=[{id:'legacy-project',title:'Open the project catalog',link:{view:'projects'}},
  {id:'file-handoff',title:'Review Aurora readme',link:{project:'aurora-console',path:'README.md'}}]
  .map(item=>({...item,status:'seen',source:'fixture',kind:'note',body:'Fictional navigation handoff.',ts:stamp}));
const json=(route,data)=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url());
  if(url.origin!==endpoint.origin||request.method()!=='GET'){
    report.writes.push(request.method()+' '+request.url());return route.abort();
  }
  if(url.pathname==='/api/xo-projects/aurora-console/file'&&url.searchParams.get('relative_path')==='fixture.html')return json(route,{
    name:'fixture.html',kind:'html',content:'<!doctype html><title>Fixture preview</title><p>Retained preview frame</p>',
    size_bytes:96,modified_at:stamp,truncated:false});
  if(url.pathname==='/api/xo-projects/aurora-console/file-history'&&url.searchParams.get('relative_path')==='fixture.html')return json(route,{is_repo:false,items:[]});
  if(url.pathname==='/api/schedules')return json(route,{jobs:[]});
  if(url.pathname==='/api/connectors/composio/toolkits')return json(route,{toolkits:[]});
  if(url.pathname==='/api/connections')return json(route,{signed_in:true,poller_enabled:true,connections:[]});
  if(url.pathname==='/api/inbox')return json(route,{items:inbox,counts:{new:0,seen:2,done:0,open:2},total:2});
  if(url.pathname==='/xo/sessions.json')return json(route,{meta:{sources:[{id:'demo',label:'Fictional runtime',available:true}]},
    totals:{sessions:3,sessions_by_agent:{demo:3}},sessions,daily_models:[],daily_sessions:[],daily_tools:[]});
  if(url.pathname==='/api/quirq')return json(route,{root:{host_path:'/demo/.quirq',readable:true,writable:true},
    totals:{files:0,bytes:0},watcher:{enabled:false},activity:{},tree:[],project_outputs:{project_count:10}});
  return route.continue();
});
const groups={projects:[['dashboard','overview','Overview'],['project-list','list','List'],['graph','graph','Graph'],
  ['tree','tree','Tree'],['sharing','sharing','Sharing'],['time','timeline','Timeline']],
  agents:['overview','sessions','tools','models','trends'].map(slug=>['agents-'+slug,slug,slug[0].toUpperCase()+slug.slice(1)]),
  inbox:['items','connections','jobs'].map(slug=>['inbox-'+slug,slug,slug[0].toUpperCase()+slug.slice(1)])};
const defaults={projects:'projects/list',agents:'agents/sessions',inbox:'inbox/items',setup:'setup/workspace'};
const aliases={projects:defaults.projects,agents:defaults.agents,inbox:defaults.inbox,setup:defaults.setup,
  dashboard:'projects/overview',list:'projects/list',graph:'projects/graph',tree:'projects/tree',sharing:'projects/sharing',
  time:'projects/timeline',timeline:'projects/timeline',sessions:'agents/sessions',
  connectors:'setup/connectors',secrets:'setup/secrets',quirq:'setup/server/details'};
const checked=text=>{report.checks.push(text);console.log(text);};
async function expectRoute(route){
  await page.waitForURL('**/#/'+route);
  const [group,slug]=route.split('/');
  const section=group==='projects'?({overview:'graph',list:'projects',graph:'graph',tree:'tree',sharing:'sharing',timeline:'time'})[slug]:route==='setup/server/details'?'quirq':group;
  await page.waitForFunction(section=>document.querySelector('#view-'+section)?.classList.contains('is-active'),section);
  assert.equal(await page.locator('.tabs [aria-current="page"]').getAttribute('id'),'tab-'+group);
  assert.equal(await page.locator('.view.is-active').count(),1,'Only one mounted view is visible');
  const mode=group==='projects'&&['overview','graph','tree'].includes(slug)?slug==='tree'?'tree':'graph':'list';
  await page.locator('#section-nav [data-view-mode="'+mode+'"][aria-current="page"]').waitFor();
  const expected=group==='projects'?[['project-list','list','Workspace'],['sharing','sharing','Sharing'],['time','timeline','Timeline']]:groups[group];
  if(expected)assert.deepEqual(await page.locator('#section-nav [data-section-page]').evaluateAll(nodes=>nodes.map(node=>[
    node.tagName,node.dataset.sectionPage,node.getAttribute('href'),node.querySelector('b').textContent])),
    expected.map(([id,leaf,label])=>['A',id,'#/'+group+'/'+leaf,label]));
  if(group==='setup'&&route!=='setup/server/details')await page.locator('#setup-panel-'+slug).waitFor({state:'visible'});
  if(group==='inbox')await page.locator('.inb-'+slug+'-page').waitFor({state:'visible'});
  if(group==='agents')await page.waitForFunction(placeholder=>
    document.querySelector('#view-search')?.placeholder===placeholder,
    slug==='sessions'?'Search loaded sessions…':'Find a page…');
}
async function go(route){await page.evaluate(route=>{location.hash='#/'+route;},route);await expectRoute(route);}
async function leaf(id){
  if(groups.projects.some(([key])=>key===id)){
    await openProjectPage(page,id==='project-list'?'projects':id);
    await expectRoute('projects/'+groups.projects.find(([key])=>key===id)[1]);return;
  }
  const link=page.locator('#section-nav [data-section-page="'+id+'"]');
  const route=(await link.getAttribute('href')).slice(2);await link.click();await expectRoute(route);
}
async function layout(label){
  const box=await page.evaluate(()=>{
    const view=document.querySelector('.view.is-active'),nav=document.querySelector('#section-nav');
    return{width:innerWidth,scroll:document.documentElement.scrollWidth,viewTop:view.getBoundingClientRect().top,
      navBottom:nav.hidden?null:nav.getBoundingClientRect().bottom,withNav:view.classList.contains('has-section-nav')};
  });
  assert.ok(box.scroll<=box.width,label+' has no document overflow');
  if(box.withNav)assert.ok(box.viewTop>=box.navBottom-1,label+' content viewport clears secondary navigation');
  report.layouts.push({label,...box});
}
async function screenshot(name){await page.mouse.move(2,998);await page.screenshot({path:resolve(output,name),animations:'disabled'});report.screenshots.push(name);}
async function previewClearsNavigation(){
  await page.waitForFunction(()=>document.querySelector('#preview').getBoundingClientRect().top
    >=document.querySelector('#section-nav').getBoundingClientRect().bottom-1);
}
async function dragPreviewUp(){
  const title=await page.locator('#preview .pv-title').boundingBox();
  await page.mouse.move(title.x+8,title.y+8);await page.mouse.down();
  await page.mouse.move(title.x+8,0,{steps:8});await page.mouse.up();
  await previewClearsNavigation();
}

try{
  await page.goto(origin+'/space/',{waitUntil:'networkidle'});await expectRoute(defaults.projects);
  assert.deepEqual(await page.locator('.tabs a').evaluateAll(nodes=>nodes.map(node=>[node.id,node.getAttribute('href')])),
    Object.entries(defaults).map(([id,route])=>['tab-'+id,'#/'+route]));
  for(const [alias,target] of Object.entries(aliases)){
    await page.goto(origin+'/space/#/'+alias,{waitUntil:'networkidle'});await expectRoute(target);
  }
  checked('Primary roots and legacy aliases normalize to canonical pages with native primary and secondary links.');
  for(const [group,pages] of Object.entries(groups)){
    await go(defaults[group]);
    for(const [id,slug] of pages){await leaf(id);await expectRoute(group+'/'+slug);}
  }
  await openProjectList(page);await page.locator('.prj-row').first().waitFor();
  await page.locator('#tab-projects').click();await expectRoute('projects/list');
  await leaf('project-list');await expectRoute('projects/list');
  const historyBefore=await page.evaluate(()=>history.length);
  await leaf('project-list');assert.equal(await page.evaluate(()=>history.length),historyBefore);
  await go('projects/overview');await leaf('project-list');
  await leaf('tree');await expectRoute('projects/tree');await page.goBack();await expectRoute('projects/list');
  await page.goBack();await expectRoute('projects/overview');await page.goForward();await expectRoute('projects/list');
  await page.evaluate(()=>{location.hash='#/tree';});await expectRoute('projects/tree');
  await page.goBack();await expectRoute('projects/list');
  checked('Projects defaults to List; scope pages and representation modes have separate identities, and browser history has one entry per navigation.');
  const search=page.locator('#view-search');
  await search.fill('aurora');
  const head=page.locator('#prj-row-aurora-console .prj-row-head');await head.click();
  await page.locator('#prjp-files [data-file="README.md"]').waitFor();
  const drawer=await page.locator('#prj-drawer-aurora-console').elementHandle();
  await page.locator('#prjp-files [data-file="README.md"]').click();
  await page.locator('#preview-body .pv-md').waitFor();
  await page.locator('#preview-version').selectOption('0');
  await page.waitForFunction(()=>document.querySelector('#preview-body')?.textContent.includes('An earlier version'));
  await page.locator('#preview-source').click();
  const preview=await page.locator('#preview-body').textContent();
  const historicalBody=await page.locator('#preview-body .pv-src').elementHandle();
  await dragPreviewUp();
  await page.evaluate(()=>{window.navigationFixtureDocument='retained-map-page';});
  for(const [id,slug] of groups.projects){
    await leaf(id);await expectRoute('projects/'+slug);
    await page.waitForFunction(text=>document.querySelector('#preview-body')?.textContent===text,preview);
    assert.equal(await page.locator('#preview.is-open').count(),1);
    assert.equal(await page.locator('#preview-source').textContent(),'Rendered');
    assert.equal(await page.evaluate(()=>window.navigationFixtureDocument),'retained-map-page','Changing atlas projections never reloads the document');
  }
  for(const width of [390,320,1440]){
    await page.setViewportSize({width,height:1000});await previewClearsNavigation();
    assert.equal(await historicalBody.evaluate(node=>node===document.querySelector('#preview-body .pv-src')),true,
      'Dragging and resizing retain the historical preview body');
    assert.equal(await page.locator('#preview-version').inputValue(),'0');
    await leaf('graph');await leaf('tree');await leaf('project-list');
  }
  await page.evaluate(()=>dispatchEvent(new CustomEvent('space:preview-file',{
    detail:{project:'aurora-console',path:'fixture.html',name:'fixture.html'}})));
  await page.locator('#preview .pv-frame').waitFor();
  const frame=await page.locator('#preview .pv-frame').elementHandle();
  await dragPreviewUp();
  for(const width of [390,320,1440]){
    await page.setViewportSize({width,height:1000});await previewClearsNavigation();
    await leaf('graph');await leaf('tree');await leaf('project-list');
    assert.equal(await frame.evaluate(node=>node===document.querySelector('#preview .pv-frame')),true,
      'Mode changes and responsive positioning retain the same preview iframe');
  }
  checked('Dragging or resizing an open preview keeps navigation usable and preserves historical content and iframe identity.');
  await leaf('project-list');assert.equal(await search.inputValue(),'aurora');
  assert.equal(await drawer.evaluate(node=>node===document.querySelector('#prj-drawer-aurora-console')),true);
  await page.locator('#tab-setup').click();await expectRoute('setup/workspace');
  assert.equal(await page.locator('#preview.is-open').count(),0);
  await openProjectList(page);await page.locator('#prj-add').click();await expectRoute('setup/projects');
  await go('setup/workspace');await page.locator('#xo-root-input').fill('/fictional/retained-draft');
  await go('projects/list');await page.locator('#prjp-files .fx-row.is-dir').first().click();
  await page.locator('#prjp-files .fx-here').waitFor();
  const folder=await page.locator('#prjp-files .fx-crumbs').textContent();
  for(const route of ['projects/overview','projects/graph','projects/timeline','projects/overview'])await go(route);
  await page.locator('#root-btn').click();await page.locator('#rootdd.is-open').waitFor();
  await page.locator('#root-btn').click();await page.waitForFunction(()=>!document.querySelector('#rootdd').classList.contains('is-open'));
  await go('projects/list');assert.equal(await page.locator('#prjp-files .fx-crumbs').textContent(),folder);
  assert.equal(await search.inputValue(),'aurora');
  await page.locator('#tab-projects').click();await expectRoute(defaults.projects);
  await page.locator('#tab-setup').click();await expectRoute(defaults.setup);
  assert.equal(await page.locator('#xo-root-input').inputValue(),'/fictional/retained-draft');
  checked('Project pages retain historical previews, List query and drawer state; leaving the section closes preview and keeps Setup drafts.');
  await go('agents/sessions');await page.locator('#sess-body tr[data-sid]').first().waitFor();
  await search.fill('Aurora');
  await leaf('agents-tools');await expectRoute('agents/tools');assert.equal(await search.getAttribute('placeholder'),'Find a page…');
  await leaf('agents-sessions');await expectRoute('agents/sessions');assert.equal(await search.inputValue(),'Aurora');
  await page.locator('#tab-agents').click();await expectRoute('agents/sessions');assert.equal(await search.inputValue(),'Aurora');
  await go('inbox/items');await page.locator('[data-id="legacy-project"][data-act="toggle"]').click();
  await page.locator('[data-id="legacy-project"][data-act="open"]').click();await expectRoute('projects/list');
  await go('inbox/items');await page.locator('[data-id="file-handoff"][data-act="toggle"]').click();
  await page.locator('[data-id="file-handoff"][data-act="open"]').click();await expectRoute('projects/list');
  await page.locator('#preview.is-open').waitFor();await page.locator('#preview-body .pv-md').waitFor();
  await page.locator('#preview-close').click();
  await go('projects/sharing');await page.locator('[data-act="list"]').first().click();await expectRoute('projects/list');
  await page.locator('.prj-row-head[aria-expanded="true"]').waitFor();
  checked('Agents page routes own their toolbar while preserving queries; Inbox legacy/file links and Sharing still open List.');
  for(const [index,[group,route]] of Object.entries(defaults).entries()){
    await page.locator('body').click({position:{x:2,y:2}});await page.keyboard.press(String(index+1));await expectRoute(route);
    assert.equal(await page.locator('#tab-'+group).getAttribute('aria-current'),'page');
  }
  await page.locator('#xo-root-input').click();
  assert.equal(await page.locator('#xo-root-input').evaluate(node=>node===document.activeElement),true);
  await page.keyboard.press('1');await expectRoute('setup/workspace');
  checked('Numbered shortcuts follow section defaults and never interrupt text entry.');
  const captures=[...groups.projects.map(([,slug])=>'projects/'+slug),'agents/overview','agents/sessions',
    'inbox/items','inbox/connections','inbox/jobs','setup/workspace'];
  for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:1000});
    for(const route of captures){
      await go(route);await page.waitForLoadState('networkidle');
      await page.locator('.view.is-active').evaluate(node=>{node.scrollTop=0;});
      await layout(route+'-'+width);await screenshot(route.replace('/','-')+'-'+width+'.png');
    }
  }
  checked('All Projects pages and representative Agents, Inbox and Setup pages fit 1440px, 390px and 320px.');
  assert.deepEqual(report.writes,[]);assert.deepEqual(report.errors,[]);
}catch(error){report.failure=error.stack;await screenshot('failure.png').catch(()=>{});throw error;}
finally{await writeFile(resolve(output,'report.json'),JSON.stringify(report,null,2));await browser.close();}
