#!/usr/bin/env node
/* Canonical section navigation over actual Space assets and fictional data.
   External requests and service mutations are blocked; one Check now POST is intercepted in memory. */
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {openProjectList,openProjectPage,projectPageSelector} from './routes.mjs';

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
const report={fixture:'Actual assets with fictional read-only responses',checks:[],screenshots:[],layouts:[],errors:[],writes:[],mockedChecks:0};
function gate(){let resolve;const promise=new Promise(done=>{resolve=done;});return{promise,resolve};}
let checkGate=null,treeGate=null;
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
  if(url.origin===endpoint.origin&&request.method()==='POST'&&url.pathname==='/api/project-sharing/check'&&checkGate){
    report.mockedChecks++;checkGate.arrived.resolve();await checkGate.release.promise;return json(route,{ok:true});
  }
  if(url.origin!==endpoint.origin||request.method()!=='GET'){
    report.writes.push(request.method()+' '+request.url());return route.abort();
  }
  if(url.pathname==='/xo/space.json'&&treeGate){
    const current=treeGate;treeGate=null;current.arrived.resolve();await current.release.promise;return route.continue();
  }
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
const groups={projects:[['dashboard','overview','Overview'],['project-list','files/list','List'],['graph','files/graph','Graph'],
  ['tree','files/tree','Tree'],['sharing','sharing','Sharing'],['time','timeline','Timeline']],
  agents:['overview','sessions','tools','models','trends'].map(slug=>['agents-'+slug,slug,slug[0].toUpperCase()+slug.slice(1)]),
  inbox:['items','connections','jobs'].map(slug=>['inbox-'+slug,slug,slug[0].toUpperCase()+slug.slice(1)])};
const defaults={projects:'projects/overview',agents:'agents/overview',inbox:'inbox/items',setup:'setup/workspace'};
const aliases={projects:defaults.projects,agents:defaults.agents,inbox:defaults.inbox,setup:defaults.setup,
  dashboard:'projects/overview',list:'projects/files/list',graph:'projects/files/graph',tree:'projects/files/tree',sharing:'projects/sharing',
  'projects/files':'projects/files/list','projects/list':'projects/files/list',
  'projects/graph':'projects/files/graph','projects/tree':'projects/files/tree',
  time:'projects/timeline',timeline:'projects/timeline',sessions:'agents/overview',
  connectors:'setup/connectors',secrets:'setup/secrets',quirq:'setup/server/details'};
const checked=text=>{report.checks.push(text);console.log(text);};
async function expectRoute(route){
  await page.waitForURL('**/#/'+route);
  const group=route.split('/')[0],definition=groups[group]?.find(([,slug])=>group+'/'+slug===route);
  assert.equal(await page.locator('.tabs [aria-current="page"]').getAttribute('id'),'tab-'+group);
  assert.equal(await page.locator('.view.is-active').count(),1,'Only one mounted view is visible');
  if(definition){
    const file=group==='projects'&&definition[1].startsWith('files/');
    const selector=group==='projects'?projectPageSelector(definition[0]):'#section-nav [data-section-page="'+definition[0]+'"]';
    await page.locator(selector+'[aria-current="page"]').waitFor();
    if(group==='projects'){
      assert.deepEqual(await page.locator('#section-nav [data-section-page]').evaluateAll(nodes=>nodes.map(node=>[node.tagName,node.dataset.sectionPage,node.textContent])),
        [['A','dashboard','Overview'],['A','files','Files'],['A','sharing','Sharing'],['A','time','Timeline']]);
      assert.equal(await page.locator('#section-nav [data-file-mode]').count(),0,'Files modes stay out of section navigation');
      assert.equal(await page.locator('.view.is-active .file-views:visible').count(),file?1:0);
      if(file){
        assert.equal(await page.locator('.view.is-active .file-views').evaluate(node=>
          !!node.closest('#view-projects .prj-head,#graph-file-toolbar,#view-tree .tv-head')),true,
          'Files modes belong to each representation toolbar');
        assert.equal(await page.locator('#section-nav [data-section-page="files"]').getAttribute('aria-current'),'page');
        assert.equal(await page.locator('#section-nav [data-section-page="files"]').getAttribute('href'),'#/'+route);
        assert.deepEqual(await page.locator('.view.is-active .file-views [data-file-mode]').evaluateAll(nodes=>nodes.map(node=>[node.tagName,node.dataset.fileMode,node.getAttribute('href'),node.textContent])),
          [['A','project-list','#/projects/files/list','List'],['A','graph','#/projects/files/graph','Graph'],['A','tree','#/projects/files/tree','Tree']]);
      }
    }else assert.deepEqual(await page.locator('#section-nav [data-section-page]').evaluateAll(nodes=>nodes.map(node=>[
      node.tagName,node.dataset.sectionPage,node.getAttribute('href'),node.textContent])),
      groups[group].map(([id,slug,label])=>['A',id,'#/'+group+'/'+slug,label]));
    assert.equal(await page.locator('#section-nav [aria-current="page"]').count(),1);
    assert.equal(await page.locator('.section-nav-label').count(),0,'Section labels are not repeated');
  }else assert.equal(await page.locator('#section-nav').isHidden(),true);
  if(group==='setup'&&route!=='setup/server/details')await page.locator('#setup-panel-'+route.split('/')[1]).waitFor({state:'visible'});
  if(group==='inbox')await page.locator('.inb-'+route.split('/')[1]+'-page').waitFor({state:'visible'});
}
async function go(route){await page.evaluate(route=>{location.hash='#/'+route;},route);await expectRoute(route);}
async function leaf(id){
  if(groups.projects.some(([key])=>key===id)){
    await openProjectPage(page,id==='project-list'?'projects':id);
    await expectRoute('projects/'+groups.projects.find(([key])=>key===id)[1]);return;
  }
  const link=page.locator('#section-nav [data-section-page="'+id+'"]');
  const route=(await link.getAttribute('href')).slice(2);
  await link.click();await expectRoute(route);
}
async function layout(label){
  const box=await page.evaluate(()=>{
    const view=document.querySelector('.view.is-active'),nav=document.querySelector('#section-nav');
    const rect=node=>{const r=node.getBoundingClientRect();return{left:r.left,right:r.right,top:r.top,bottom:r.bottom};};
    const projects=nav.dataset.section==='projects';
    return{width:innerWidth,scroll:document.documentElement.scrollWidth,viewTop:view.getBoundingClientRect().top,
      navBottom:nav.hidden?null:nav.getBoundingClientRect().bottom,withNav:view.classList.contains('has-section-nav'),
      scopes:projects?[...nav.querySelectorAll('.section-nav-links > a')].map(rect):[],
      scopeArea:projects?rect(nav.querySelector('.section-nav-links')):null,
      modes:view.querySelector('.file-views')?.getClientRects().length?rect(view.querySelector('.file-views')):null,
      actions:projects?rect(nav.querySelector('.section-nav-actions')):null,
      sharing:[...nav.querySelectorAll('.sharing-page-actions button')].filter(node=>node.getClientRects().length).map(rect),
      manage:projects?rect(nav.querySelector('.section-nav-action')):null};
  });
  assert.ok(box.scroll<=box.width,label+' has no document overflow');
  if(box.withNav)assert.ok(box.viewTop>=box.navBottom-1,label+' content viewport clears secondary navigation');
  for(const scope of box.scopes)assert.ok(scope.left>=box.scopeArea.left-1&&scope.right<=box.scopeArea.right+1,
    label+' scope links remain fully visible');
  if(box.scopes.length&&box.width<=480){
    assert.ok(box.scopes.every(scope=>Math.abs(scope.top-box.scopes[0].top)<1),label+' four scopes share the first phone row');
    assert.ok(Math.max(...box.scopes.map(scope=>scope.bottom))<=box.actions.top+1,
      label+' root controls follow the scope links');
  }
  for(const action of box.sharing){
    assert.ok(action.left>=-1&&action.right<=box.width+1&&action.bottom<=box.navBottom+1,label+' Sharing actions fit the section bar');
    assert.ok(Math.abs((action.bottom-action.top)-(box.manage.bottom-box.manage.top))<2,label+' Sharing and Manage have equal control heights');
    if(box.width>=1440)assert.ok(Math.abs(action.top-box.manage.top)<2,label+' Sharing and Manage align on desktop');
  }
  if(box.modes)assert.ok(box.modes.left>=-1&&box.modes.right<=box.width+1,label+' local Files modes fit the viewport');
  report.layouts.push({label,...box});
}
async function screenshot(name){await page.mouse.move(2,998);await page.screenshot({path:resolve(output,name),animations:'disabled'});report.screenshots.push(name);}

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
  await page.locator('#tab-projects').click();await expectRoute('projects/overview');
  await leaf('project-list');await expectRoute('projects/files/list');
  const historyBefore=await page.evaluate(()=>history.length);
  await leaf('project-list');assert.equal(await page.evaluate(()=>history.length),historyBefore);
  await leaf('tree');await expectRoute('projects/files/tree');await page.goBack();await expectRoute('projects/files/list');
  await page.goBack();await expectRoute('projects/overview');await page.goForward();await expectRoute('projects/files/list');
  await page.evaluate(()=>{location.hash='#/tree';});await expectRoute('projects/files/tree');
  await page.goBack();await expectRoute('projects/files/list');
  await leaf('tree');await leaf('sharing');
  assert.equal(await page.locator('#section-nav [data-section-page="files"]').getAttribute('href'),'#/projects/files/tree');
  await page.locator('#section-nav [data-section-page="files"]').click();await expectRoute('projects/files/tree');
  await leaf('project-list');
  checked('Projects defaults to Overview; List has a separate identity, and browser history has one entry per navigation.');
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
  await page.evaluate(()=>{window.navigationFixtureDocument='retained-map-page';});
  for(const [id,slug] of groups.projects){
    await leaf(id);await expectRoute('projects/'+slug);
    await page.waitForFunction(text=>document.querySelector('#preview-body')?.textContent===text,preview);
    assert.equal(await page.locator('#preview.is-open').count(),1);
    assert.equal(await page.locator('#preview-source').textContent(),'Rendered');
    assert.equal(await page.evaluate(()=>window.navigationFixtureDocument),'retained-map-page','Changing atlas projections never reloads the document');
  }
  await leaf('project-list');assert.equal(await search.inputValue(),'aurora');
  assert.equal(await drawer.evaluate(node=>node===document.querySelector('#prj-drawer-aurora-console')),true);
  await page.locator('#tab-setup').click();await expectRoute('setup/workspace');
  assert.equal(await page.locator('#preview.is-open').count(),0);
  await openProjectList(page);await page.locator('#prj-add').click();await expectRoute('setup/projects');
  await go('setup/workspace');await page.locator('#xo-root-input').fill('/fictional/retained-draft');
  await go('projects/files/list');await page.locator('#prjp-files .fx-row.is-dir').first().click();
  await page.locator('#prjp-files .fx-here').waitFor();
  const folder=await page.locator('#prjp-files .fx-crumbs').textContent();
  for(const route of ['projects/overview','projects/files/graph','projects/timeline','projects/overview'])await go(route);
  await page.locator('#root-btn').click();await page.locator('#rootdd.is-open').waitFor();
  await page.locator('#root-btn').click();await page.waitForFunction(()=>!document.querySelector('#rootdd').classList.contains('is-open'));
  await go('projects/files/list');assert.equal(await page.locator('#prjp-files .fx-crumbs').textContent(),folder);
  assert.equal(await search.inputValue(),'aurora');
  await page.locator('#tab-projects').click();await expectRoute(defaults.projects);
  await page.locator('#tab-setup').click();await expectRoute(defaults.setup);
  assert.equal(await page.locator('#xo-root-input').inputValue(),'/fictional/retained-draft');
  checked('Project pages retain historical previews, List query and drawer state; leaving the section closes preview and keeps Setup drafts.');
  await go('agents/sessions');await page.locator('#sess-body tr[data-sid]').first().waitFor();
  await search.fill('Aurora');
  await leaf('agents-tools');await expectRoute('agents/tools');assert.equal(await search.isVisible(),false);
  await leaf('agents-sessions');await expectRoute('agents/sessions');assert.equal(await search.inputValue(),'Aurora');
  await page.locator('#tab-agents').click();await expectRoute('agents/overview');assert.equal(await search.isVisible(),false);
  await go('inbox/items');await page.locator('[data-id="legacy-project"][data-act="toggle"]').click();
  await page.locator('[data-id="legacy-project"][data-act="open"]').click();await expectRoute('projects/files/list');
  await go('inbox/items');await page.locator('[data-id="file-handoff"][data-act="toggle"]').click();
  await page.locator('[data-id="file-handoff"][data-act="open"]').click();await expectRoute('projects/files/list');
  await page.locator('#preview.is-open').waitFor();await page.locator('#preview-body .pv-md').waitFor();
  await go('projects/sharing');await page.locator('[data-act="list"]').first().click();await expectRoute('projects/files/list');
  await page.locator('.prj-row-head[aria-expanded="true"]').waitFor();
  checked('Agents page routes own their toolbar while preserving queries; Inbox legacy/file links and Sharing still open List.');
  for(const [index,[group,route]] of Object.entries(defaults).entries()){
    await page.locator('body').click({position:{x:2,y:2}});await page.keyboard.press(String(index+1));await expectRoute(route);
    assert.equal(await page.locator('#tab-'+group).getAttribute('aria-current'),'page');
  }
  await page.locator('#xo-root-input').focus();
  assert.equal(await page.locator('#xo-root-input').evaluate(node=>node===document.activeElement),true);
  await page.keyboard.press('1');await expectRoute('setup/workspace');
  checked('Numbered shortcuts follow section defaults and never interrupt text entry.');
  await go('projects/sharing');
  const shareAction=page.locator('#section-nav .sharing-page-actions [data-act="composer"]');
  const checkAction=page.locator('#section-nav .sharing-page-actions [data-act="check"]');
  await page.waitForFunction(()=>!document.querySelector('#section-nav [data-act="composer"]')?.disabled);
  assert.equal(await page.locator('#view-sharing > .prj > .prj-head [data-act]').count(),0);
  const actionsNode=await page.locator('.sharing-page-actions').elementHandle();
  await shareAction.focus();await shareAction.press('Enter');
  await page.locator('#shl-composer').waitFor();assert.equal(await shareAction.textContent(),'Cancel');
  assert.equal(await page.locator('#shl-composer [data-filter]').evaluate(node=>node===document.activeElement),true);
  await page.locator('#shl-composer [data-filter]').fill('Aurora');
  await page.locator('#shl-composer input[name="ws"]').fill('fictional-workspace-draft');
  const composerNode=await page.locator('#shl-composer').elementHandle();
  checkGate={arrived:gate(),release:gate()};await checkAction.click();
  await Promise.race([checkGate.arrived.promise,new Promise((_,reject)=>setTimeout(()=>reject(new Error('Check now did not reach its in-memory fixture')),5000))]);
  assert.equal(await checkAction.isDisabled(),true);assert.equal(await checkAction.getAttribute('aria-busy'),'true');
  assert.equal(await checkAction.textContent(),'Checking…');
  await checkAction.evaluate(node=>node.click());assert.equal(report.mockedChecks,1,'Busy checks cannot submit twice');
  await leaf('project-list');assert.equal(await page.locator('#section-nav .sharing-page-actions').count(),0);
  await leaf('sharing');assert.equal(await checkAction.isDisabled(),true);
  assert.equal(await actionsNode.evaluate(node=>node===document.querySelector('.sharing-page-actions')),true);
  checkGate.release.resolve();await page.waitForFunction(()=>!document.querySelector('#section-nav [data-act="check"]').disabled);
  assert.equal(await checkAction.getAttribute('aria-busy'),null);
  assert.equal(await composerNode.evaluate(node=>node===document.querySelector('#shl-composer')),true,'Check now leaves the editor mounted');
  assert.equal(await page.locator('#shl-composer input[name="ws"]').inputValue(),'fictional-workspace-draft');
  await shareAction.click();assert.equal(await page.locator('#shl-composer').count(),0);
  checked('Sharing actions align with section controls, retain their nodes across pages, toggle the composer and prevent duplicate checks while preserving its draft.');

  await leaf('tree');await page.locator('[data-tv="reload"]').waitFor();
  treeGate={arrived:gate(),release:gate()};const delayedTree=treeGate;
  await page.locator('[data-tv="reload"]').click();
  await Promise.race([delayedTree.arrived.promise,new Promise((_,reject)=>setTimeout(()=>reject(new Error('Tree Refresh did not reach its fixture')),5000))]);
  await page.locator('.view.is-active .file-views [data-file-mode="project-list"]').click();await expectRoute('projects/files/list');
  delayedTree.release.resolve();await page.waitForLoadState('networkidle');await expectRoute('projects/files/list');
  checked('Files mode links remain usable during a delayed Tree refresh, and its late read cannot reclaim List.');
  const captures=[...groups.projects.map(([,slug])=>'projects/'+slug),'agents/overview','agents/sessions',
    'inbox/items','inbox/connections','inbox/jobs','setup/workspace'];
  for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:1000});
    for(const route of captures){
      await go(route);await page.waitForLoadState('networkidle');
      await page.locator('.view.is-active').evaluate(node=>{node.scrollTop=0;});
      await layout(route+'-'+width);await screenshot(route.replaceAll('/','-')+'-'+width+'.png');
    }
  }
  checked('All Projects pages and representative Agents, Inbox and Setup pages fit 1440px, 390px and 320px.');
  assert.deepEqual(report.writes,[]);assert.deepEqual(report.errors,[]);
}catch(error){report.failure=error.stack;await screenshot('failure.png').catch(()=>{});throw error;}
finally{await writeFile(resolve(output,'report.json'),JSON.stringify(report,null,2));await browser.close();}
