/* Inbox Jobs and the shared results drawer, using read-only browser fixtures.
   No command, Inbox item, connection, or server process is changed. */
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5100';
assert.equal(new URL(origin).hostname,'127.0.0.1');
assert.notEqual(new URL(origin).port,'5002');
const output=resolve(process.argv[2]||'/tmp/space-inbox-jobs-review');
const captureOnly=process.argv.includes('--capture-only');
await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE
  ?pathToFileURL(process.env.PLAYWRIGHT_MODULE).href:'playwright');
const browser=await chromium.launch({headless:true,
  executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000}});
const page=await context.newPage(),errors=[],reads=[];
page.on('pageerror',error=>errors.push(error.message));
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
const gate=()=>({arrived:deferred(),release:deferred()});
let holdList=gate(),failure=null;
const initial=holdList;
if(captureOnly)holdList=null;
const result={status:'ok',duration_seconds:0.24,returncode:0,trigger:'schedule',
  started_at:'2026-09-14T10:00:00Z',finished_at:'2026-09-14T10:00:01Z',
  output_tail:'Fictional scheduler output <img src=x onerror="window.jobsXss=true">'};
const job=(id,name,seconds)=>({id,name,every_seconds:seconds,enabled:true,
  description:'Check the fictional workspace and keep its result.',
  command:{argv:['python3','scripts/check.py'],cwd:'/demo/workspace',timeout:30},
  next_run:'2026-09-14T10:30:00Z',running:false,running_since:null,last_result:result});
const seeded=[job('manual','Manual command',null),job('half-minute','Check release readiness',30),
  {...job('ninety-seconds','Paused delivery audit',90),enabled:false,last_result:{...result,status:'failed'}},
  {...job('hourly','Build reference catalog',3600),running:true,running_since:'2026-09-14T10:00:00Z'},
  job('daily','Daily workspace summary',86400)];
let jobs=structuredClone(seeded);
const inbox=[{id:'release',title:'Release handoff',kind:'note',source:'agent',status:'seen',
  ts:'2026-09-14T10:00:00Z',body:Array.from({length:45},(_,i)=>`Fictional handoff detail ${i+1}`).join('\n')},
  {id:'new-issue',title:'Improve the guide',kind:'issue',source:'issues',status:'new',
    ts:'2026-09-14T10:00:00Z',body:'Review the fictional navigation guide.'}];
const send=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url());
  assert.equal(url.origin,new URL(origin).origin,'No external services');
  assert.equal(request.method(),'GET','Inbox Jobs and opening results never write or run commands');
  if(url.pathname==='/api/schedules'){
    reads.push({path:url.pathname,time:Date.now()});
    const snapshot=structuredClone(jobs),error=failure,pending=holdList;holdList=null;
    if(pending){pending.arrived.resolve();await pending.release.promise;}
    return send(route,error?{detail:error}:{jobs:snapshot},error?503:200);
  }
  const match=url.pathname.match(/^\/api\/schedules\/([^/]+)(\/runs)?$/);
  if(match){
    reads.push({path:url.pathname,time:Date.now()});
    const item=jobs.find(job=>job.id===match[1]);
    if(!item)return send(route,{detail:'Unknown job'},404);
    return send(route,match[2]?{job_id:item.id,log_path:`/demo/.quirq/scheduler/logs/${item.id}.log`,runs:[result]}:item);
  }
  if(url.pathname==='/api/connections')return send(route,{connections:[{
    toolkit:'calendar',display_name:'Calendar',configured:true,connected_here:true,enabled:true,
    interval_s:900,collectors:['events'],last_poll_at:'2026-09-14T10:00:00Z',last_error:null,
  }]});
  if(url.pathname==='/api/inbox')return send(route,{items:inbox,counts:{new:1,seen:1,done:0},total:2});
  await route.continue();
});
const rows=page.locator('.inb-job-row');
const item=id=>page.locator(`[data-job-id="${id}"]`);
const refresh=()=>page.locator('[data-act="jobs-refresh"]').click();
const inboxPage=name=>page.locator('#section-nav [href="#/inbox/'+name+'"]');
async function settled(){await page.locator('.inb-jobs[aria-busy="false"]').waitFor();}
async function expectCount(count){await page.waitForFunction(count=>document.querySelectorAll('.inb-job-row').length===count,count);}
try{
  await page.goto(origin+'/space/#/inbox/'+(captureOnly?'jobs':'items'),{waitUntil:'domcontentloaded'});
  if(!captureOnly){
  await page.locator('.inb-row').first().waitFor();
  assert.equal(reads.filter(read=>read.path==='/api/schedules').length,0,'Items loads independently of Jobs');
  const countSummary=await page.locator('.inb-sum').textContent();
  await page.locator('#view-search').fill('handoff');
  assert.equal(await page.locator('.inb-row').count(),1);
  assert.equal(await page.locator('.inb-sum').textContent(),countSummary);
  assert.equal(await page.locator('[data-act="mark-all"]').textContent(),'Mark all loaded seen');
  await page.locator('[data-act="toggle"][data-id="release"]').click();
  await page.locator('#inb-body-release .inb-text').evaluate(el=>el.scrollTop=100);
  const savedScroll=await page.locator('#inb-body-release .inb-text').evaluate(el=>el.scrollTop);
  await page.locator('[data-act="toggle"][data-id="release"]').evaluate(el=>window.retainedInboxHead=el);
  await inboxPage('connections').click();
  await page.locator('.inb-conn-row').waitFor();
  assert.equal(await page.locator('[data-act="conns-toggle"]').getAttribute('aria-expanded'),'true');
  assert.equal(await page.locator('.inb-items-page').isVisible(),false);
  await inboxPage('jobs').click();await initial.arrived.promise;
  assert.match(await page.locator('.inb-jobs').textContent(),/Loading scheduled jobs/);
  assert.equal(await page.locator('.inb-connections-page').isVisible(),false);
  assert.equal(await page.locator('#view-search').isVisible(),false,'Item search is absent from Jobs');
  initial.release.resolve();await expectCount(4);await settled();
  assert.equal(await page.locator('[data-job-id="manual"]').count(),0);
  assert.match(await item('half-minute').textContent(),/Every 30 s/);
  assert.match(await item('ninety-seconds').textContent(),/Every 90 s[\s\S]*Disabled[\s\S]*failed/);
  assert.match(await item('hourly').textContent(),/Every 1 h[\s\S]*Running/);
  assert.match(await item('daily').textContent(),/Every 1 d/);
  assert.match(await item('half-minute').textContent(),/Next due/);
  assert.equal(await page.evaluate(()=>document.querySelector('[data-act="toggle"][data-id="release"]')===window.retainedInboxHead),true);
  jobs.find(job=>job.id==='hourly').running=false;
  const beforePoll=Date.now();
  await page.waitForFunction(()=>!document.querySelector('[data-job-id="hourly"] .is-running'),null,{timeout:7000});
  assert.ok(Date.now()-beforePoll>=2000,'Running scheduled jobs refresh on the 3s poll');
  assert.equal(await page.evaluate(()=>document.querySelector('[data-act="toggle"][data-id="release"]')===window.retainedInboxHead),true,'Jobs polling does not rebuild Inbox items');
  await inboxPage('items').click();
  await page.locator('.inb-items-page').waitFor();
  assert.equal(await page.locator('#inb-body-release .inb-text').evaluate(el=>el.scrollTop),savedScroll,'Item scroll survives time spent on Jobs');
  assert.equal(await page.locator('#view-search').inputValue(),'handoff');
  assert.equal(await page.locator('.inb-row').count(),1);
  assert.equal(await page.locator('.inb-sum').textContent(),countSummary);
  await inboxPage('jobs').click();await settled();
  assert.equal(await rows.count(),4,'Item query does not filter Jobs');

  jobs.find(job=>job.id==='half-minute').name='Stale schedule response';
  const slow=holdList=gate();await refresh();await slow.arrived.promise;
  jobs.find(job=>job.id==='half-minute').name='Current release readiness';
  await refresh();
  slow.release.resolve();await settled();
  await page.waitForFunction(()=>document.querySelector('[data-job-id="half-minute"] h3')?.textContent==='Current release readiness');
  assert.equal(await page.locator('[data-act="jobs-refresh"]').evaluate(el=>el===document.activeElement),true,'Refresh focus survives updated jobs');

  failure='Scheduler temporarily unavailable <img src=x>';
  await refresh();await settled();
  assert.match(await page.locator('.inb-jobs-state.is-error').textContent(),/showing the last good read/);
  assert.equal(await rows.count(),4);assert.equal(await page.locator('.inb-jobs img').count(),0);
  failure=null;jobs=seeded.filter(job=>job.every_seconds==null);
  await refresh();await settled();await expectCount(0);
  assert.match(await page.locator('.inb-jobs').textContent(),/No scheduled jobs/);
  jobs=structuredClone(seeded);jobs.find(job=>job.id==='hourly').running=false;
  await refresh();await expectCount(4);await settled();

  const away=holdList=gate();await refresh();await away.arrived.promise;
  await page.locator('#wiki-link').click();
  jobs.find(job=>job.id==='half-minute').name='Fresh after returning';
  await page.locator('#tab-inbox').click();
  await page.locator('.inb-items-page').waitFor();
  assert.equal(await page.locator('#view-search').inputValue(),'handoff');
  assert.equal(await page.locator('[data-act="toggle"][data-id="release"]').getAttribute('aria-expanded'),'true');
  await inboxPage('jobs').click();
  away.release.resolve();await settled();
  await page.waitForFunction(()=>document.querySelector('[data-job-id="half-minute"] h3')?.textContent==='Fresh after returning');

  const listReads=reads.filter(read=>read.path==='/api/schedules').length;
  await page.waitForFunction(()=>document.querySelector('.inb-jobs[aria-busy="false"]'));
  const idleStart=Date.now();
  while(reads.filter(read=>read.path==='/api/schedules').length===listReads){
    assert.ok(Date.now()-idleStart<35000,'Idle scheduled jobs poll within30s');
    await page.waitForTimeout(250);
  }
  assert.ok(Date.now()-idleStart>=28000,'Idle polling uses30s, without a rapid request loop');
  await settled();
  jobs.find(job=>job.id==='half-minute').running=true;
  await refresh();await settled();
  for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:1000});
    await page.locator('.inb-jobs').scrollIntoViewIfNeeded();
    assert.ok(await page.locator('.inb-jobs,.inb-jobs button,.inb-job-info').evaluateAll(nodes=>nodes.every(el=>{
      const box=el.getBoundingClientRect();return box.left>=0&&box.right<=innerWidth;
    })),`${width}px Jobs fits the viewport`);
    await page.screenshot({path:resolve(output,`inbox-jobs-${width}.png`)});
    await item('half-minute').locator('[data-act="job-results"]').click();
    await page.locator('.setup-run pre').waitFor();
    if(width===1440){
      jobs.find(job=>job.id==='half-minute').running=false;
      await page.waitForFunction(()=>!document.querySelector('[data-job-id="half-minute"] .is-running'),null,{timeout:7000});
    }
    assert.match(await page.locator('#command-runs-title').textContent(),/Fresh after returning/);
    assert.match(await page.locator('.setup-run pre').textContent(),/<img src=x/);
    assert.equal(await page.locator('#command-runs img').count(),0);
    assert.equal(await page.evaluate(()=>window.jobsXss),undefined);
    assert.ok(await page.locator('#command-runs,#command-runs-close,#command-runs pre').evaluateAll(nodes=>nodes.every(el=>{
      const box=el.getBoundingClientRect();return box.left>=0&&box.right<=innerWidth;
    })),`${width}px shared results drawer fits the viewport`);
    await page.screenshot({path:resolve(output,`inbox-job-results-${width}.png`)});
    await page.keyboard.press('1');assert.equal(new URL(page.url()).hash,'#/inbox/jobs');
    await page.keyboard.press('Escape');await page.locator('#command-runs').waitFor({state:'hidden'});
    await page.waitForFunction(()=>document.activeElement===document.querySelector('[data-job-id="half-minute"] [data-act="job-results"]'));
    assert.equal(await item('half-minute').locator('[data-act="job-results"]').evaluate(el=>el===document.activeElement),true);
  }
  failure='Scheduler temporarily unavailable';
  await page.reload({waitUntil:'networkidle'});
  await settled();
  assert.equal(await page.locator('.inb-row').count(),0,'A Jobs deep link does not fetch Items');
  assert.match(await page.locator('.inb-jobs-state.is-error').textContent(),/Could not load scheduled jobs/);
  assert.doesNotMatch(await page.locator('.inb-jobs').textContent(),/showing the last good read/);
  assert.equal(await rows.count(),0);
  await inboxPage('items').click();await page.locator('.inb-row').first().waitFor();
  assert.equal(await page.locator('.inb-row').count(),2,'Items remains available when Jobs fails');
  await inboxPage('jobs').click();await settled();
  failure=null;await refresh();await settled();await expectCount(4);
  assert.ok(reads.some(read=>read.path==='/api/schedules/half-minute/runs'));
  }else{
    await expectCount(4);await settled();
  }

  /* Publication captures use readable fictional output; the adversarial
     output above is only a rendering regression fixture. */
  result.output_tail='Checked 12 projects.\nNo stale references found.\nAll workspace checks passed.';
  jobs=[
    {...job('release-ready','Check release readiness',60),description:'Check pending changes before the next release.'},
    {...job('reference-catalog','Build reference catalog',3600),description:'Validate documentation links and refresh the local index.'},
    {...job('daily-summary','Daily workspace summary',86400),description:'Summarize completed work and surface blocked projects.'},
  ];
  await page.setViewportSize({width:1440,height:1000});
  await refresh();await settled();await expectCount(3);
  await page.locator('.inb').evaluate(el=>el.scrollIntoView({block:'start'}));
  await page.screenshot({path:resolve(output,'pr-inbox-jobs.png')});
  await item('release-ready').locator('[data-act="job-results"]').click();
  await page.locator('.setup-run pre').waitFor();
  await page.screenshot({path:resolve(output,'pr-inbox-results.png')});
  await page.locator('#command-runs-close').click();
  await page.locator('[data-act="jobs-setup"]').click();
  await page.locator('#setup-panel-commands').waitFor();
  assert.equal(await page.locator('#setup-nav [data-setup-go="commands"]').getAttribute('aria-current'),'step','Inbox opens command management directly');
  await page.locator('[data-command-id="release-ready"]').waitFor();
  await page.locator('#setup-commands').evaluate(el=>el.scrollIntoView({block:'start'}));
  assert.equal(await page.locator('#command-form').isVisible(),false);
  await page.locator('#tab-setup.is-on').waitFor();
  await page.waitForTimeout(600); /* allow the view's opacity transition to settle */
  await page.screenshot({path:resolve(output,'pr-setup-commands.png')});
  assert.deepEqual(errors,[]);
  console.log(captureOnly?'Captured fictional Inbox Jobs, shared Results and Setup Commands.':'Inbox Jobs: scheduled-only rows, precise intervals, independent loading, 3s/30s polls, refresh/reentry races, retained focus/expansion/search, empty/error states and shared Results pass at1440/390/320px.');
}catch(error){
  await page.screenshot({path:resolve(output,'failure.png')});throw error;
}finally{
  await writeFile(resolve(output,'report.json'),JSON.stringify({errors,reads},null,2)+'\n');
  await browser.close();
}
