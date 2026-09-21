#!/usr/bin/env node
/* The real Projects UI against fictional, browser-owned API fixtures. No
   request can mutate a service or leave the local preview origin. */
import assert from 'node:assert/strict';
import {openProjectList,openProjectPage} from './routes.mjs';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5100';
const endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');
assert.ok(!['5002','5112'].includes(endpoint.port),'Use the read-only preview server');
const output=resolve(process.argv[2]||'/tmp/space-projects-experience');
await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE
  ?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const browser=await chromium.launch({headless:true,
  executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000},
  deviceScaleFactor:1,locale:'en-US',timezoneId:'UTC',reducedMotion:'reduce'});
await context.addInitScript(()=>{Date.now=()=>Date.parse('2026-09-14T10:00:00Z');});
const page=await context.newPage();
page.setDefaultTimeout(15000);
const report={origin,fixture:'Fictional catalog and API responses; no service mutations',
  checks:[],requests:[],writes:[],errors:[],screenshots:[],layouts:[]};
const stamp='2026-09-14T09:45:00Z';
const catalog=[
  ['aurora-console','Aurora Console','Release visibility and customer signals.'],
  ['orbit-api','Orbit API','Typed contracts and dependable event delivery.'],
  ['field-notes','Field Notes','Offline notebooks for field research.'],
  ['harbor-infra','Harbor Infrastructure','Deployment recipes and disaster recovery.'],
  ['signal-watch','Signal Watch','Alerts and runbooks for service reliability.'],
  ['atlas-handbook','Atlas Handbook','Team agreements and a welcoming onboarding guide.'],
  ['retrieval-lab','Retrieval Lab','Reproducible search quality experiments.'],
  ['launch-studio','Launch Studio','Product stories and the next launch presentation.'],
].map(([id,display_name,description],index)=>({id,display_name,description,
  path:'/fictional/projects/'+id,created_at:`2026-09-${String(index+1).padStart(2,'0')}T10:00:00Z`,unscaffolded:false}));
const graph={hubs:catalog.map(project=>({id:'p_'+project.id})),
  leaves:catalog.flatMap((project,index)=>Array.from({length:8+index},(_,i)=>({path:project.id+'/src/file-'+i+'.ts'})))};
const session=id=>({project_id:id,session_id:'fixture-'+id,agent:'workspace',runtime:'local',
  opened_at:'2026-09-14T08:00:00Z',last_activity_at:stamp});
const events=id=>({events:[{project_id:id,type:'project.updated',runtime:'local',ts:stamp}]});
const projectActivity={open_sessions:[session('aurora-console'),session('orbit-api')]};
function tree(id,relative=''){
  return{project_id:id,relative_path:relative,parent_relative_path:'',
    dirs:relative?[]:[{name:'src',relative_path:'src',entries:3},{name:'docs',relative_path:'docs',entries:2}],
    files:Array.from({length:relative?4:28},(_,i)=>({name:i===0?(relative?'implementation.ts':'README.md'):'review-note-'+String(i).padStart(2,'0')+'.md',
      relative_path:(relative?relative+'/':'')+(i===0?(relative?'implementation.ts':'README.md'):'review-note-'+String(i).padStart(2,'0')+'.md'),
      size_bytes:1000+i*80,modified_at:stamp}))};
}
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
const gate=()=>({arrived:deferred(),release:deferred(),done:deferred()});
async function within(promise,label){
  let timer;
  try{return await Promise.race([promise,new Promise((_,reject)=>{
    timer=setTimeout(()=>reject(new Error('Timed out: '+label)),15000);
  })]);}finally{clearTimeout(timer);}
}
const queued=new Map();
const allGates=[];
function hold(key,data,status=200){const pending=gate();allGates.push(pending);const queue=queued.get(key)||[];queue.push({pending,data,status});queued.set(key,queue);return pending;}
function requestKey(url){
  const path=url.pathname;
  if(path.endsWith('/tree'))return path+(url.searchParams.get('relative_path')?'?relative_path='+url.searchParams.get('relative_path'):'');
  return path;
}
const json=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
const graphHold=hold('/xo/space.json',graph);
const activityHold=hold('/api/xo-projects/activity',projectActivity);
const timelineHold=hold('/api/xo-projects/timeline',{events:catalog.map(project=>events(project.id).events[0])});
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),path=url.pathname,method=request.method();
  if(url.origin!==endpoint.origin||method!=='GET'){
    report.writes.push(method+' '+request.url());return route.abort();
  }
  const key=requestKey(url);report.requests.push({path,key,query:url.search,method});
  const response=queued.get(key)?.shift();
  if(response){
    response.pending.arrived.resolve();await response.pending.release.promise;
    await json(route,response.data,response.status);response.pending.done.resolve();return;
  }
  if(path==='/api/xo-projects')return json(route,{items:catalog,total:catalog.length});
  if(path==='/xo/space.json')return json(route,graph);
  if(path==='/xo/dashboard.json')return json(route,graph);
  if(path==='/api/xo-projects/activity')return json(route,projectActivity);
  if(path==='/api/xo-projects/timeline')return json(route,{events:catalog.map(project=>events(project.id).events[0])});
  const detail=path.match(/^\/api\/xo-projects\/([^/]+)\/(tree|todos|activity|timeline|github\/issues)$/);
  if(detail){
    const [,id,kind]=detail;
    if(kind==='tree')return json(route,tree(id,url.searchParams.get('relative_path')||''));
    assert.fail('Files drawers must not request '+kind);
  }
  // Non-Projects shell and Setup reads are equally fictional.
  const ancillary={
    '/space/server/status':{running:true},
    '/api/inbox':{schema:1,generated_at:'2026-09-14T10:00:00Z',runner:{enabled:true},sections:[],rows:[],count:0},
    '/api/connections':{signed_in:false,poller_enabled:false,connections:[]},
    '/api/schedules':{jobs:[],items:[],scheduler_enabled:false},
    '/api/secrets':{items:[]},
    '/api/runtime-config':{configured:{agent_name:'fixture',watcher_enabled:false,watcher_source_mode:'all',watcher_interval_seconds:30},
      applied:{agent_name:'fixture',watcher_enabled:false,watcher_source_mode:'all',watcher_interval_seconds:30},
      agents:[],restart_required:false,restart_supported:false},
    '/space/setup/status':{space:{status:'configured',id:'fixture-space',label:'Review workspace',owner:'Demo owner'},
      xo:{status:'not_configured'},github:{status:'not_configured'}},
    '/space/update/status':{supported:false,message:'Fictional review server'},
  };
  if(path in ancillary)return json(route,ancillary[path]);
  if(path.startsWith('/api/')||path.startsWith('/xo/')||path.startsWith('/xo-auth/')){
    report.errors.push('Unmocked API read '+path);return json(route,{detail:'Fixture has no response'},404);
  }
  return route.continue();
});
page.on('pageerror',error=>report.errors.push(error.message));
page.on('console',message=>{
  if(message.type()!=='error')return;
  if(message.location().url.startsWith(origin+'/api/xo-projects')&&/\b503\b/.test(message.text()))return;
  report.errors.push(message.text());
});
const row=id=>page.locator('#prj-row-'+id);
const aurora=row('aurora-console');
const drawer=()=>page.locator('#prj-drawer-aurora-console');
const body=key=>drawer().locator('[data-panel="'+key+'"]');
const search=page.locator('#view-search');
const count=key=>report.requests.filter(request=>request.key===key).length;
const detailCounts=()=>Object.fromEntries(['tree','todos','activity','timeline','github/issues'].map(key=>[key,
  report.requests.filter(request=>request.path==='/api/xo-projects/aurora-console/'+key).length]));
const visibleIDs=()=>page.locator('.prj-row:visible').evaluateAll(rows=>rows.map(row=>row.id.replace('prj-row-','')));
const checked=message=>{report.checks.push(message);console.log(message);};
async function selectFilter(filter){await page.locator('#prj-filter').selectOption(filter);}
async function screenshot(name){
  await page.mouse.move(2,990);await page.screenshot({path:resolve(output,name),animations:'disabled'});
  report.screenshots.push(name);
}
async function layout(label){
  const dimensions=await page.evaluate(()=>({width:innerWidth,document:document.documentElement.scrollWidth,
    page:document.querySelector('#view-projects').clientWidth,pageScroll:document.querySelector('#view-projects').scrollWidth,
    pageTop:document.querySelector('#view-projects').getBoundingClientRect().top,
    localModes:!!document.querySelector('#view-projects .prj-head .data-views'),
    filterBeforeSort:document.querySelector('#prj-filter').getBoundingClientRect().left<document.querySelector('#prj-sort').getBoundingClientRect().left,
    navBottom:document.querySelector('#section-nav').getBoundingClientRect().bottom}));
  assert.ok(dimensions.document<=dimensions.width,label+' has no document overflow');
  assert.ok(dimensions.localModes,label+' Data modes belong to the local toolbar');
  assert.ok(dimensions.filterBeforeSort,label+' Filter precedes Sort by');
  assert.ok(dimensions.pageScroll<=dimensions.page+1,label+' has no Projects overflow');
  assert.ok(dimensions.pageTop>=dimensions.navBottom-1,label+' scroll viewport clears shared navigation');
  const outside=await page.locator('#view-projects button:visible,#view-projects input:visible,#view-projects select:visible').evaluateAll(nodes=>nodes.filter(node=>{
    const bounds=node.getBoundingClientRect();return bounds.width&&bounds.height&&(bounds.left< -1||bounds.right>innerWidth+1);
  }).map(node=>node.id||node.className));
  assert.deepEqual(outside,[],label+' controls stay inside the viewport');
  report.layouts.push({label,...dimensions});
}

try{
  await page.goto(origin+'/space/#/projects/data/list',{waitUntil:'domcontentloaded'});
  await aurora.waitFor({timeout:5000});
  await within(Promise.all([graphHold.arrived.promise,activityHold.arrived.promise,timelineHold.arrived.promise]),'optional summary requests start');
  assert.equal(await page.locator('.prj-row:visible').count(),catalog.length);
  assert.deepEqual(detailCounts(),{tree:0,todos:0,activity:0,timeline:0,'github/issues':0});
  checked('The catalog is usable while graph counts, live activity and timeline reads are still pending.');
  graphHold.release.resolve();activityHold.release.resolve();timelineHold.release.resolve();
  await within(Promise.all([graphHold.done.promise,activityHold.done.promise,timelineHold.done.promise]),'optional summaries finish');
  await selectFilter('live');
  await page.waitForFunction(()=>document.querySelectorAll('.prj-row:not([hidden])').length===2);
  assert.deepEqual((await visibleIDs()).sort(),['aurora-console','orbit-api']);
  await selectFilter('all');
  await search.fill('disaster recovery');await row('harbor-infra').waitFor();
  await page.waitForFunction(()=>document.querySelectorAll('.prj-row:not([hidden])').length===1);
  assert.deepEqual(await visibleIDs(),['harbor-infra'],'Descriptions participate in search');
  await search.fill('not-a-project-fixture');await page.getByText('No matching projects',{exact:true}).waitFor();
  await page.locator('#view-projects').getByRole('button',{name:'Show all projects',exact:true}).click();
  assert.equal(await search.inputValue(),'');assert.equal((await visibleIDs()).length,catalog.length);
  assert.equal(await page.locator('#prj-filter').inputValue(),'all');
  checked('All and Live filters, description search, and the empty-results clear action work without detail requests.');

  assert.equal(await page.locator('#view-projects .prj-pin,#view-projects .prj-share,#view-projects .prj-map,#view-projects .prj-row-actions').count(),0,
    'Data rows reserve their actions for opening the file browser');
  const pin=()=>page.locator('[data-project-pin="aurora-console"]');
  await openProjectPage(page,'manage');await pin().click();
  assert.equal(await pin().getAttribute('aria-pressed'),'true');
  assert.equal(await pin().evaluate(node=>node===document.activeElement),true);
  await openProjectList(page);await selectFilter('pinned');assert.deepEqual(await visibleIDs(),['aurora-console']);
  await page.reload({waitUntil:'networkidle'});await selectFilter('pinned');await aurora.waitFor();
  assert.deepEqual(await visibleIDs(),['aurora-console']);
  await openProjectPage(page,'manage');assert.equal(await pin().getAttribute('aria-pressed'),'true');await pin().click();
  assert.equal(await pin().getAttribute('aria-pressed'),'false');
  await openProjectList(page);await page.getByText('Keep your frequent projects here',{exact:true}).waitFor();
  assert.match(await page.locator('#view-projects').textContent(),/Pin projects in Manage/);
  await page.locator('#view-projects').getByRole('button',{name:'Show all projects',exact:true}).click();
  assert.equal(await page.locator('#prj-filter').inputValue(),'all','Clearing an empty pinned view resets the native Filter');
  await openProjectPage(page,'manage');await pin().click();await openProjectList(page);
  const filter=page.locator('#prj-filter');await filter.focus();await filter.press('p');await filter.press('Enter');
  assert.equal(await filter.inputValue(),'pinned','The native filter supports keyboard selection');
  assert.deepEqual(await visibleIDs(),['aurora-console']);
  await selectFilter('all');
  await page.locator('#prj-sort').selectOption('name');
  assert.equal((await visibleIDs())[0],'atlas-handbook');
  checked('Manage pins persist across reload, update Data Pinned filters, and leave Data rows free of management actions.');

  const header=aurora.locator('.prj-row-head');await header.focus();await page.keyboard.press('Enter');
  await body('files').locator('[data-file="README.md"]').waitFor();
  assert.equal(await header.getAttribute('aria-expanded'),'true');
  assert.equal(await header.evaluate(node=>node===document.activeElement),true);
  assert.deepEqual(detailCounts(),{tree:1,todos:0,activity:0,timeline:0,'github/issues':0});
  const savedDrawer=await drawer().elementHandle();
  const filePane=body('files').locator('.fx-files');await filePane.evaluate(node=>{node.scrollTop=120;});
  const fileScroll=await filePane.evaluate(node=>node.scrollTop);assert.ok(fileScroll>0,'Fixture file list scrolls');
  const beforeSort=detailCounts();
  await page.locator('#prj-sort').selectOption('created');
  await search.fill('customer signals');await aurora.waitFor();
  await page.waitForFunction(()=>document.querySelectorAll('.prj-row:not([hidden])').length===1);
  assert.equal(await savedDrawer.evaluate(node=>node===document.querySelector('#prj-drawer-aurora-console')),true);
  assert.equal(await filePane.evaluate(node=>node.scrollTop),fileScroll);
  assert.deepEqual(detailCounts(),beforeSort,'Sorting and search do not refetch file contents');
  await search.fill('');await selectFilter('pinned');await selectFilter('all');
  assert.equal(await savedDrawer.evaluate(node=>node===document.querySelector('#prj-drawer-aurora-console')),true);
  assert.equal(await filePane.evaluate(node=>node.scrollTop),fileScroll);
  await header.focus();await page.keyboard.press('Enter');
  assert.equal(await header.getAttribute('aria-expanded'),'false');
  assert.equal(await header.evaluate(node=>node===document.activeElement),true,'Collapse retains keyboard focus');
  await page.keyboard.press('Space');await body('files').locator('[data-file="README.md"]').waitFor();
  assert.deepEqual(detailCounts(),beforeSort,'Reopening retains the already loaded file browser');
  checked('Keyboard expansion and collapse retain focus; sort/search/filter changes preserve drawer DOM, file scroll, and loaded requests.');

  assert.equal(await drawer().locator('[data-project-tab],[data-project-group="activity"],[data-project-group="issues"]').count(),0,
    'Files has no redundant detail tabs or hidden Activity/Issues groups');
  checked('Expanded Files rows contain only the file browser, with no Activity or Issues requests.');

  const oldTree=hold('/api/xo-projects/aurora-console/tree',tree('aurora-console'));
  await drawer().locator('.prj-detail-refresh').click();await within(oldTree.arrived.promise,'older root-tree refresh starts');
  await body('files').locator('[data-cd="src"]').click();
  await body('files').locator('[data-file="src/implementation.ts"]').waitFor();
  oldTree.release.resolve();await within(oldTree.done.promise,'older root-tree refresh completes');await page.waitForTimeout(50);
  assert.equal(await body('files').locator('[data-file="src/implementation.ts"]').count(),1);
  assert.equal(await body('files').locator('[data-file="README.md"]').count(),0);
  checked('A late root-folder refresh cannot replace a newer same-project folder navigation.');

  await openProjectPage(page,'manage');await page.waitForURL('**/#/projects/manage');
  await page.locator('#manage-project-add').waitFor();assert.deepEqual(report.writes,[]);
  await openProjectList(page);await page.waitForURL('**/#/projects/data/list');
  await body('files').locator('[data-file="src/implementation.ts"]').waitFor();
  checked('The Manage page opens project management and returning restores the current drawer and folder.');

  await selectFilter('pinned');await search.fill('customer signals');
  await page.evaluate(()=>dispatchEvent(new CustomEvent('space:open-project',{detail:'field-notes'})));
  await row('field-notes').locator('.prj-row-head[aria-expanded="true"]').waitFor();
  assert.equal(await search.inputValue(),'');
  assert.equal(await page.locator('#prj-filter').inputValue(),'all');
  assert.equal(await row('field-notes').locator('.prj-row-head').evaluate(node=>node===document.activeElement),true);
  await header.click();
  checked('The Sharing project handoff clears both query and view filter, opens its project, and focuses its row.');

  await body('files').locator('.fx-crumb[data-cd=""]').click();
  await body('files').locator('[data-file="README.md"]').waitFor();
  await page.locator('#prj-sort').selectOption('name');await selectFilter('all');await search.fill('');
  for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:1000});
    if(await header.getAttribute('aria-expanded')==='true')await header.click();
    await page.locator('#view-projects').evaluate(node=>{node.scrollTop=0;});
    await layout('list-'+width);await screenshot('projects-list-'+width+'.png');
    await header.click();await drawer().scrollIntoViewIfNeeded();
    await layout('files-'+width);await screenshot('projects-files-'+width+'.png');
    await search.fill('not-a-project-fixture');await page.getByText('No matching projects',{exact:true}).waitFor();
    await page.locator('#view-projects').evaluate(node=>{node.scrollTop=0;});
    await layout('empty-'+width);await screenshot('projects-empty-'+width+'.png');
    await page.locator('#view-projects').getByRole('button',{name:'Show all projects',exact:true}).click();
  }
  checked('List, Files and empty results fit 1440px, 390px and 320px with no overflowing controls.');
  const beforeFailure=await aurora.elementHandle();
  const refreshFailure=hold('/api/xo-projects',{detail:'Catalog temporarily unavailable'},503);
  await page.locator('#project-refresh').click();await within(refreshFailure.arrived.promise,'catalog refresh failure arrives');
  refreshFailure.release.resolve();await within(refreshFailure.done.promise,'catalog refresh failure completes');
  await page.locator('#prj-status').getByText(/Showing the last list/).waitFor();
  assert.equal((await visibleIDs()).length,catalog.length);
  assert.equal(await beforeFailure.evaluate(node=>node===document.querySelector('#prj-row-aurora-console')),true);
  await page.locator('#project-refresh').click();
  await page.waitForFunction(()=>!document.querySelector('#project-refresh').disabled&&document.querySelector('#prj-status').hidden);
  const initialFailure=hold('/api/xo-projects',{detail:'Catalog temporarily unavailable'},503);
  await page.reload({waitUntil:'domcontentloaded'});await within(initialFailure.arrived.promise,'initial catalog failure arrives');
  initialFailure.release.resolve();await within(initialFailure.done.promise,'initial catalog failure completes');
  await page.getByText('Projects could not load',{exact:true}).waitFor();
  assert.equal((await visibleIDs()).length,0);
  await page.locator('[data-retry-projects]').click();await aurora.waitFor();
  assert.equal((await visibleIDs()).length,catalog.length);
  checked('Initial catalog errors offer retry; later refresh failures preserve existing rows and recover.');
  const emptyCatalog=hold('/api/xo-projects',{items:[],total:0});
  await page.reload({waitUntil:'domcontentloaded'});await within(emptyCatalog.arrived.promise,'empty catalog arrives');
  emptyCatalog.release.resolve();await within(emptyCatalog.done.promise,'empty catalog completes');
  await page.getByText('No projects yet',{exact:true}).waitFor();
  await page.locator('[data-first-run]').click();
  await page.waitForURL('**/#/wiki');
  await page.waitForFunction(()=>document.activeElement?.id==='wiki-quickstart');
  checked('An empty workspace offers Getting started and opens the focused Wiki quickstart.');
  assert.deepEqual(report.writes,[],'Every request remains a read-only fixture');
  assert.deepEqual(report.errors,[],'No browser or fixture errors');
}catch(error){
  report.failure=error.stack;await page.screenshot({path:resolve(output,'failure.png')}).catch(()=>{});throw error;
}finally{
  // Release any unconsumed fixture gates before closing the browser.
  for(const pending of allGates)pending.release.resolve();
  await writeFile(resolve(output,'report.json'),JSON.stringify(report,null,2));await browser.close();
}
