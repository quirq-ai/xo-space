#!/usr/bin/env node
/* The Work tab's Activity page over intercepted fictional reads. */
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5101',endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');assert.ok(!['5002','5112'].includes(endpoint.port));
const output=resolve(process.argv[2]||'/private/tmp/space-inbox-activity');await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
const page=await context.newPage();page.setDefaultTimeout(15000);
const report={checks:[],screenshots:[],requests:[],errors:[],writes:[]};
const gate=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
const holds=new Map(),allHolds=[];
function hold(path){const value={arrived:gate(),release:gate()};holds.set(path,value);allHolds.push(value);return value;}
const catalogHold=hold('/api/xo-projects'),liveHold=hold('/api/xo-projects/activity');
const timestamp='2026-09-14T09:00:00Z',cursor='2026-09-13T09:00:00Z';
const malicious='<img src=x onerror="window.activityInjected=true">';
const events=[
  {id:'file',project_id:'aurora-console',type:'file.edited',ts:timestamp,path:'src/app.ts',runtime:'codex',payload:{authorization:'fixture-secret-not-rendered'}},
  {id:'todo',project_id:'aurora-console',type:'todo.completed',ts:'2026-09-14T08:00:00Z',todo:{content:'Review accessible navigation'}},
  {id:'session',project_id:'orbit-api',type:'session.closed',ts:'2026-09-14T07:00:00Z',session_id:'fixture-session',outcome:'Complete'},
  {id:'escaped',project_id:'orbit-api',type:'file.created',ts:'invalid',path:malicious},null,
];
const older={id:'older',project_id:'aurora-console',type:'project.created',ts:'2026-09-12T09:00:00Z',title:'Earlier workspace milestone'};
let workspaceFailure=false,liveFailure=false,todosFailure=false;
const json=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),path=url.pathname;
  if(url.origin!==endpoint.origin||request.method()!=='GET'){report.writes.push(request.method()+' '+request.url());return route.abort();}
  report.requests.push(path+url.search);
  const waiting=holds.get(path);if(waiting){holds.delete(path);waiting.arrived.resolve();await waiting.release.promise;}
  if(path==='/api/xo-projects')return json(route,{items:[{id:'aurora-console',display_name:'Aurora Console'},{id:'orbit-api',display_name:'Orbit API'}]});
  if(path==='/api/xo-projects/activity')return json(route,liveFailure?{detail:'fixture-secret-in-error'}:{open_sessions:[
    {session_id:'live-a',project_id:'aurora-console',runtime:'codex',last_activity_at:timestamp},
    {session_id:'live-b',project_id:'orbit-api',runtime:'claude_code',last_activity_at:timestamp},
  ]},liveFailure?503:200);
  const scoped=path.match(/^\/api\/xo-projects\/([^/]+)\/(activity|todos)$/);
  if(scoped){
    const [,project,kind]=scoped;
    if(kind==='activity')return json(route,liveFailure?{detail:'fixture-secret-in-error'}:{project_id:project,open_sessions:[
      {session_id:'scoped-'+project,agent:'Workspace agent',runtime:'codex',opened_at:timestamp,last_activity_at:timestamp},
    ]},liveFailure?503:200);
    if(todosFailure)return json(route,{detail:'fixture-secret-in-error'},503);
    return json(route,{project_id:project,sessions:{'fictional-session':{runtime:'codex',todos:[
      {id:'done',status:'completed',content:'Completed '+project+' review'},
      {id:'active',status:'in_progress',content:'Current '+project+' work'},
      {id:'blocked',status:'blocked',content:'Blocked '+project+' task'},
      {id:'pending',status:'pending',content:'Pending '+project+' note'},
      {id:'cancelled',status:'cancelled',content:'Cancelled '+project+' experiment'},
    ]}}});
  }
  if(/^\/api\/xo-projects(?:\/[^/]+)?\/timeline$/.test(path)){
    if(workspaceFailure)return json(route,{detail:'fixture-secret-in-error'},503);
    const project=path.match(/^\/api\/xo-projects\/([^/]+)\/timeline$/)?.[1];
    const rows=url.searchParams.has('before')?[events[0],older]:events;
    return json(route,{events:project?rows.filter(event=>event?.project_id===project):rows,
      next_cursor:project||url.searchParams.has('before')?null:cursor});
  }
  return route.continue();
});
page.on('pageerror',error=>report.errors.push(error.message));
await page.addInitScript(()=>{window.activityDocument='same-document';window.activityInjected=false;});
const view=kind=>page.locator('#view-inbox-'+kind);
const rows=kind=>view(kind).locator('.iac-event');
const query=()=>page.locator('#view-search');
const select=kind=>view(kind).locator('[data-activity-project-filter]');
const checked=text=>{report.checks.push(text);console.log(text);};
async function go(kind){
  await page.evaluate(kind=>{location.hash='#/inbox/'+kind;},kind);
  await view(kind).waitFor({state:'visible'});
  await page.waitForFunction(()=>document.querySelector('#view-search')?.placeholder==='Search activity…');
  await page.waitForFunction(()=>!document.querySelector('#section-refresh').disabled);
}
async function refresh(){await page.locator('#section-refresh').click();await page.waitForFunction(()=>!document.querySelector('#section-refresh').disabled);}
async function rowCount(kind,count){await page.waitForFunction(({kind,count})=>document.querySelectorAll('#view-inbox-'+kind+' .iac-event').length===count,{kind,count});}
try{
  await page.goto(origin+'/space/#/inbox/activity',{waitUntil:'domcontentloaded'});
  await view('activity').waitFor({state:'visible'});await rowCount('activity',4);
  assert.match(await view('activity').locator('[data-activity-warning]').textContent(),/Some activity records/);
  assert.equal(await view('activity').locator('img,script').count(),0);
  assert.equal(await page.evaluate(()=>window.activityInjected),false);
  assert.doesNotMatch(await view('activity').textContent(),/fixture-secret-not-rendered/);
  assert.match(await rows('activity').last().textContent(),/Time unavailable/);
  catalogHold.release.resolve();liveHold.release.resolve();
  await page.waitForFunction(()=>!document.querySelector('#section-refresh').disabled);
  await view('activity').locator('[data-activity-live-summary]').getByText('2 open sessions',{exact:true}).waitFor();
  await view('activity').locator('[data-activity-live]').click();
  assert.match(await view('activity').locator('[data-activity-live-rows]').textContent(),/Aurora Console/);
  assert.equal(report.requests.some(path=>path.endsWith('/todos')),false,'All projects does not fetch every project todo list');
  checked('Workspace events render before optional names/live reads; malformed rows are reported, timestamps stay truthful, and event content cannot execute or expose unrelated fields.');

  await view('activity').locator('[data-activity-more]').click();await rowCount('activity',5);
  assert.equal(report.requests.some(path=>path.includes('before='+encodeURIComponent(cursor))),true);
  assert.equal(await view('activity').locator('[data-activity-more]').isVisible(),false);
  await query().fill('Aurora');await rowCount('activity',3);
  await select('activity').selectOption('orbit-api');await rowCount('activity',0);
  await query().fill('Orbit');await rowCount('activity',2);
  assert.equal(report.requests.includes('/api/xo-projects/orbit-api/timeline?limit=200'),true);
  await view('activity').locator('[data-activity-todos-summary]').getByText(/5/).waitFor();
  const todoRows=view('activity').locator('[data-activity-todo-rows]');
  assert.deepEqual(await todoRows.locator('.iac-todo-status').allTextContents(),['in progress','pending','blocked','completed','cancelled']);
  assert.match(await todoRows.textContent(),/Current orbit-api work/);
  assert.match(await view('activity').locator('[data-activity-live-rows]').textContent(),/Workspace agent/);
  assert.ok(report.requests.includes('/api/xo-projects/orbit-api/activity'));
  assert.ok(report.requests.includes('/api/xo-projects/orbit-api/todos'));
  checked('Load older follows the server cursor and deduplicates events; project selection requests that project history and intersects loaded-event search.');

  await page.evaluate(()=>{location.hash='#/inbox/items';});await page.locator('#view-inbox').waitFor({state:'visible'});
  assert.equal(await page.locator('#view-inbox-sharing-activity').count(),0,'Sharing activity is no longer a page');
  await go('activity');assert.equal(await query().inputValue(),'Orbit');assert.equal(await select('activity').inputValue(),'orbit-api');
  await rowCount('activity',2);
  checked('Activity keeps its search and project selection while other Work pages are shown; Sharing activity is gone.');

  workspaceFailure=true;liveFailure=true;todosFailure=true;await refresh();await rowCount('activity',2);
  assert.match(await view('activity').locator('[data-activity-warning]').textContent(),/Previously loaded events.*Open sessions are unavailable/);
  assert.doesNotMatch(await view('activity').textContent(),/fixture-secret/);
  assert.match(await view('activity').locator('[data-activity-warning]').textContent(),/todos/);
  workspaceFailure=false;liveFailure=false;todosFailure=false;await refresh();
  assert.equal(await view('activity').locator('[data-activity-warning]').isVisible(),false);
  checked('Failed refreshes preserve readable history, identify unavailable live data, hide raw error payloads, and recover through the shared Refresh action.');

  await query().fill('');await select('activity').selectOption('');await rowCount('activity',4);
  const waiting=hold('/api/xo-projects/orbit-api/timeline');
  await select('activity').selectOption('orbit-api');await waiting.arrived.promise;
  await select('activity').selectOption('aurora-console');await rowCount('activity',2);
  waiting.release.resolve();await page.waitForLoadState('networkidle');
  assert.equal(await select('activity').inputValue(),'aurora-console');assert.match(await rows('activity').first().textContent(),/Aurora Console/);
  assert.match(await todoRows.textContent(),/Current aurora-console work/);
  assert.doesNotMatch(await todoRows.textContent(),/orbit-api/);
  checked('A late history reply for an older project selection cannot replace the current project feed or selected todos.');

  for(const kind of ['activity']){
    await go(kind);await query().fill('');await select(kind).selectOption('');
    for(const width of [1440,390,320]){
      await page.setViewportSize({width,height:1000});
      // ResizeObserver publishes the shell inset on the next rendering turn.
      await page.evaluate(()=>new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done))));
      await view(kind).evaluate(node=>{node.scrollTop=0;});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      const bound=await view(kind).boundingBox(),nav=await page.locator('#section-nav').boundingBox();
      assert.ok(bound.y>=nav.y+nav.height-1,kind+' clears navigation at '+width);
      const name=kind+'-'+width+'.png';await page.screenshot({path:resolve(output,name),animations:'disabled'});report.screenshots.push(name);
    }
  }
  assert.equal(await page.evaluate(()=>window.activityDocument),'same-document');
  checked('The Activity page fits desktop and phone layouts.');
  assert.deepEqual(report.errors,[]);assert.deepEqual(report.writes,[]);
}catch(error){report.failure=error.stack;await page.screenshot({path:resolve(output,'failure.png')}).catch(()=>{});throw error;}
finally{allHolds.forEach(hold=>hold.release.resolve());await writeFile(resolve(output,'report.json'),JSON.stringify(report,null,2));await browser.close();}
