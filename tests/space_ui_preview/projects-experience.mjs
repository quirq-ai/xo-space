#!/usr/bin/env node
/* The real Projects UI against fictional, browser-owned API fixtures. No
   request can mutate a service or leave the local preview origin. */
import assert from 'node:assert/strict';
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
function issues(id,{stale=false}={}){
  return{project_id:id,state:'ok',repo:'fictional/'+id,tracked:1,fetched_at:stamp,
    issues:[
      {number:101,title:stale?'Stale mirror issue':'Improve keyboard navigation',state:'open',labels:['accessibility'],assignees:[{login:'demo-dev'}],updated_at:stamp,url:'https://github.com/fictional/'+id+'/issues/101'},
      {number:102,title:'Tighten layout on phones',state:'open',labels:['design'],assignees:[],updated_at:stamp},
      {number:90,title:'Restore keyboard focus after closing',state:'closed',labels:['accessibility'],assignees:[],updated_at:stamp},
    ]};
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
  if(path.endsWith('/github/issues'))return path+(url.searchParams.get('refresh')==='1'?'?refresh=1':'');
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
  if(path==='/api/xo-projects/activity')return json(route,projectActivity);
  if(path==='/api/xo-projects/timeline')return json(route,{events:catalog.map(project=>events(project.id).events[0])});
  const detail=path.match(/^\/api\/xo-projects\/([^/]+)\/(tree|todos|activity|timeline|github\/issues)$/);
  if(detail){
    const [,id,kind]=detail;
    if(kind==='tree')return json(route,tree(id,url.searchParams.get('relative_path')||''));
    if(kind==='todos')return json(route,{project_id:id,sessions:{demo:{runtime:'local',todos:[
      {id:'task-1',content:'Review release checklist',status:'in_progress'},
      {id:'task-2',content:'Confirm responsive layouts',status:'pending'},
      {id:'task-3',content:'Publish the updated guide',status:'completed'},
    ]}}});
    if(kind==='activity')return json(route,{open_sessions:[session(id)]});
    if(kind==='timeline')return json(route,events(id));
    return json(route,issues(id));
  }
  // Non-Projects shell and Setup reads are equally fictional.
  const ancillary={
    '/space/server/status':{running:true},
    '/api/inbox':{items:[],counts:{new:0,seen:0,done:0,open:0},total:0},
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
const body=key=>page.locator('#prjp-'+key);
const search=page.locator('#view-search');
const count=key=>report.requests.filter(request=>request.key===key).length;
const detailCounts=()=>Object.fromEntries(['tree','todos','activity','timeline','github/issues'].map(key=>[key,
  report.requests.filter(request=>request.path==='/api/xo-projects/aurora-console/'+key).length]));
const visibleIDs=()=>page.locator('.prj-row:visible').evaluateAll(rows=>rows.map(row=>row.id.replace('prj-row-','')));
const checked=message=>{report.checks.push(message);console.log(message);};
async function selectGroup(group){await drawer().locator('[data-project-tab="'+group+'"]').click();}
async function selectFilter(filter){await page.locator('[data-project-filter="'+filter+'"]').click();}
async function screenshot(name){
  await page.mouse.move(2,990);await page.screenshot({path:resolve(output,name),animations:'disabled'});
  report.screenshots.push(name);
}
async function layout(label){
  const dimensions=await page.evaluate(()=>({width:innerWidth,document:document.documentElement.scrollWidth,
    page:document.querySelector('#view-projects').clientWidth,pageScroll:document.querySelector('#view-projects').scrollWidth,
    pageTop:document.querySelector('#view-projects').getBoundingClientRect().top,
    lensBottom:document.querySelector('#fileslens .atlas-lens-switch').getBoundingClientRect().bottom}));
  assert.ok(dimensions.document<=dimensions.width,label+' has no document overflow');
  assert.ok(dimensions.pageScroll<=dimensions.page+1,label+' has no Projects overflow');
  assert.ok(dimensions.pageTop>=dimensions.lensBottom+8,label+' scroll viewport stays below the lens switch');
  const outside=await page.locator('#view-projects button:visible,#view-projects input:visible,#view-projects select:visible').evaluateAll(nodes=>nodes.filter(node=>{
    const bounds=node.getBoundingClientRect();return bounds.width&&bounds.height&&(bounds.left< -1||bounds.right>innerWidth+1);
  }).map(node=>node.id||node.className));
  assert.deepEqual(outside,[],label+' controls stay inside the viewport');
  report.layouts.push({label,...dimensions});
}

try{
  await page.goto(origin+'/space/#/projects',{waitUntil:'domcontentloaded'});
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
  checked('All and Live filters, description search, and the empty-results clear action work without detail requests.');

  await aurora.locator('.prj-pin').click();
  assert.equal(await aurora.locator('.prj-pin').getAttribute('aria-pressed'),'true');
  await selectFilter('pinned');assert.deepEqual(await visibleIDs(),['aurora-console']);
  await page.reload({waitUntil:'networkidle'});
  await selectFilter('pinned');await aurora.waitFor();
  assert.deepEqual(await visibleIDs(),['aurora-console']);
  assert.equal(await aurora.locator('.prj-pin').getAttribute('aria-pressed'),'true');
  await selectFilter('all');
  await page.locator('#prj-sort').selectOption('name');
  assert.equal((await visibleIDs())[0],'atlas-handbook');
  checked('Pins persist across a reload, Pinned filters correctly, and the sort menu orders projects.');

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
  assert.deepEqual(detailCounts(),beforeSort,'Sorting and search do not refetch detail groups');
  await search.fill('');await selectFilter('pinned');await selectFilter('all');
  assert.equal(await savedDrawer.evaluate(node=>node===document.querySelector('#prj-drawer-aurora-console')),true);
  assert.equal(await filePane.evaluate(node=>node.scrollTop),fileScroll);
  await header.focus();await page.keyboard.press('Enter');
  assert.equal(await header.getAttribute('aria-expanded'),'false');
  assert.equal(await header.evaluate(node=>node===document.activeElement),true,'Collapse retains keyboard focus');
  await page.keyboard.press('Space');await body('files').locator('[data-file="README.md"]').waitFor();
  assert.deepEqual(detailCounts(),beforeSort,'Reopening retains the already loaded Files group');
  checked('Keyboard expansion and collapse retain focus; sort/search/filter changes preserve drawer DOM, file scroll, and loaded requests.');

  await selectGroup('activity');await body('todos').getByText('Review release checklist').waitFor();
  await body('activity').getByText('workspace',{exact:true}).waitFor();
  await body('timeline').getByText('project.updated').waitFor();
  assert.deepEqual(detailCounts(),{tree:1,todos:1,activity:1,timeline:1,'github/issues':0});
  const beforeRefresh=detailCounts();await drawer().locator('.prj-detail-refresh').click();
  await page.waitForFunction(()=>!document.querySelector('.prj-detail-refresh')?.disabled);
  await page.waitForTimeout(50);
  assert.deepEqual(detailCounts(),{tree:beforeRefresh.tree,todos:2,activity:2,timeline:2,'github/issues':0});
  checked('Activity lazily loads Todos, Open sessions and Events; group refresh only reloads those three sources.');

  const oldIssues=hold('/api/xo-projects/aurora-console/github/issues',issues('aurora-console',{stale:true}));
  await selectGroup('issues');await within(oldIssues.arrived.promise,'initial Issues mirror request');
  await drawer().locator('.prj-detail-refresh').click();
  await body('issues').getByText('Improve keyboard navigation').waitFor();
  oldIssues.release.resolve();await within(oldIssues.done.promise,'older Issues mirror completes');
  await page.waitForTimeout(50);
  assert.equal(await body('issues').getByText('Stale mirror issue').count(),0,'Older mirror response cannot replace a newer forced refresh');
  assert.equal(count('/api/xo-projects/aurora-console/github/issues?refresh=1'),1);
  await body('issues').locator('[data-iss-state="all"]').click();
  const issueSearch=body('issues').locator('.iss-q');await issueSearch.fill('keyboard');
  await page.waitForFunction(()=>document.querySelectorAll('#prjp-issues .iss-row').length===2);
  const searchNode=await issueSearch.elementHandle(),issuesBefore=detailCounts();
  await selectGroup('files');await selectGroup('activity');await selectGroup('issues');
  assert.equal(await issueSearch.inputValue(),'keyboard');
  assert.equal(await body('issues').locator('[data-iss-state="all"]').getAttribute('aria-pressed'),'true');
  assert.equal(await searchNode.evaluate(node=>node===document.querySelector('#prjp-issues .iss-q')),true);
  assert.deepEqual(detailCounts(),issuesBefore);
  checked('Issues retain local filters and controls across tabs; late mirror data cannot overwrite a newer refresh.');

  await selectGroup('files');
  const oldTree=hold('/api/xo-projects/aurora-console/tree',tree('aurora-console'));
  await drawer().locator('.prj-detail-refresh').click();await within(oldTree.arrived.promise,'older root-tree refresh starts');
  await body('files').locator('[data-cd="src"]').click();
  await body('files').locator('[data-file="src/implementation.ts"]').waitFor();
  oldTree.release.resolve();await within(oldTree.done.promise,'older root-tree refresh completes');await page.waitForTimeout(50);
  assert.equal(await body('files').locator('[data-file="src/implementation.ts"]').count(),1);
  assert.equal(await body('files').locator('[data-file="README.md"]').count(),0);
  checked('A late root-folder refresh cannot replace a newer same-project folder navigation.');

  await page.locator('#prj-add').click();await page.waitForURL('**/#/setup/projects');
  await page.locator('#setup-projects').waitFor();assert.deepEqual(report.writes,[]);
  await page.locator('#tab-projects').click();await page.waitForURL('**/#/projects');
  await body('files').locator('[data-file="src/implementation.ts"]').waitFor();
  checked('Add project opens canonical Setup Projects and returning restores the current drawer and folder.');

  await selectFilter('pinned');await search.fill('customer signals');
  await page.evaluate(()=>dispatchEvent(new CustomEvent('space:open-project',{detail:'field-notes'})));
  await row('field-notes').locator('.prj-row-head[aria-expanded="true"]').waitFor();
  assert.equal(await search.inputValue(),'');
  assert.equal(await page.locator('[data-project-filter="all"]').getAttribute('aria-pressed'),'true');
  assert.equal(await row('field-notes').locator('.prj-row-head').evaluate(node=>node===document.activeElement),true);
  await header.click();await selectGroup('files');
  checked('The Sharing project handoff clears both query and view filter, opens its project, and focuses its row.');

  await body('files').locator('.fx-crumb[data-cd=""]').click();
  await body('files').locator('[data-file="README.md"]').waitFor();
  await page.locator('#prj-sort').selectOption('name');await selectFilter('all');await search.fill('');
  for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:1000});
    if(await header.getAttribute('aria-expanded')==='true')await header.click();
    await page.locator('#view-projects').evaluate(node=>{node.scrollTop=0;});
    await layout('list-'+width);await screenshot('projects-list-'+width+'.png');
    await header.click();await selectGroup('files');await drawer().scrollIntoViewIfNeeded();
    await layout('files-'+width);await screenshot('projects-files-'+width+'.png');
    await selectGroup('activity');await drawer().scrollIntoViewIfNeeded();
    await layout('activity-'+width);await screenshot('projects-activity-'+width+'.png');
    await search.fill('not-a-project-fixture');await page.getByText('No matching projects',{exact:true}).waitFor();
    await page.locator('#view-projects').evaluate(node=>{node.scrollTop=0;});
    await layout('empty-'+width);await screenshot('projects-empty-'+width+'.png');
    await page.locator('#view-projects').getByRole('button',{name:'Show all projects',exact:true}).click();
  }
  checked('List, Files, Activity and empty results fit 1440px, 390px and 320px with no overflowing controls.');
  const beforeFailure=await aurora.elementHandle();
  const refreshFailure=hold('/api/xo-projects',{detail:'Catalog temporarily unavailable'},503);
  await page.locator('#prj-refresh').click();await within(refreshFailure.arrived.promise,'catalog refresh failure arrives');
  refreshFailure.release.resolve();await within(refreshFailure.done.promise,'catalog refresh failure completes');
  await page.locator('#prj-status').getByText(/Showing the last list/).waitFor();
  assert.equal((await visibleIDs()).length,catalog.length);
  assert.equal(await beforeFailure.evaluate(node=>node===document.querySelector('#prj-row-aurora-console')),true);
  await page.locator('#prj-refresh').click();
  await page.waitForFunction(()=>!document.querySelector('#prj-refresh').disabled&&document.querySelector('#prj-status').hidden);
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
