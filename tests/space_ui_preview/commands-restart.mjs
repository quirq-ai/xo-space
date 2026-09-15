/* Browser-only API fixtures: never executes a command or restarts a server.
   Start tests/space_ui_preview/server.py, then run this script with Playwright.
   SPACE_PREVIEW_URL may point to another isolated local fixture server. */
import assert from 'node:assert/strict';
import {mkdir} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5100';
const endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');
assert.notEqual(endpoint.port,'5002','Never target the real workspace server');
const output=resolve(process.argv[2]||'/tmp/space-commands-review');
await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE
  ?pathToFileURL(process.env.PLAYWRIGHT_MODULE).href:'playwright');
const browser=await chromium.launch({headless:true,
  executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000}});
const page=await context.newPage();
const errors=[];
page.on('pageerror',error=>errors.push(error.message));
page.on('dialog',dialog=>dialog.accept());
const clone=value=>structuredClone(value);
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
const fixtureRuntime=await (await context.request.get(origin+'/api/runtime-config')).json();
fixtureRuntime.configured.watcher_enabled=false;
fixtureRuntime.applied.watcher_enabled=false;
fixtureRuntime.restart_required=true;
let restartMode='foreground',instanceId='fixture-before',restartReject=false;
const writes=[],polls=[];
let holdPut=null,holdList=null,nextId=3;
const result=status=>({status,started_at:'2026-09-14T10:00:00Z',finished_at:'2026-09-14T10:00:02Z',
  duration_seconds:2.5,trigger:'manual',returncode:status==='ok'?0:2,
  output_tail:'<img src=x onerror="window.fixtureXss=true">\nFictional command output & diagnostics.',
  reason:status==='timed_out'?'Timeout reached':null});
const job=(id,name)=>({id,name,description:'Read-only fixture command <b>literal text</b>',
  command:{argv:['git','status','--short'],timeout:30,cwd:'/demo/workspace',env:{DEMO_MODE:'fixture'}},
  every_seconds:null,enabled:true,project_id:null,running:false,running_since:null,last_result:null});
let jobs=[job('job-a','Check checkout'),job('job-b','Build guide')];
const history=new Map();
const pollCounts=new Map();
const send=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),path=url.pathname;
  assert.equal(url.origin,endpoint.origin,'Fixtures cannot call external services');
  const method=request.method();
  if(method!=='GET')writes.push({path,method,body:request.postDataJSON()});
  if(path==='/api/runtime-config'&&method==='GET')return send(route,fixtureRuntime);
  if(path==='/space/server/status')return send(route,{status:'on',instance_id:instanceId,restart_mode:restartMode});
  if(path==='/space/server/restart')return send(route,restartReject?{detail:'A restart is already in progress'}
    :{ok:true,restarting:true,mode:restartMode,instance_id:instanceId},restartReject?409:200);
  if(path==='/space/update/status')return send(route,{supported:true,fetch_ok:true,up_to_date:false,
    branch:'fixture',behind:1,ahead:0,dirty:false,current:{sha:'before',date:'today',subject:'Fixture'},
    latest:{sha:'after',date:'today',subject:'Fixture update'}});
  if(path==='/space/update/apply')return send(route,{updated:true,to:{sha:'after',date:'today',subject:'Fixture update'},
    commits:1,message:'Fixture response only.',requirements_changed:false});
  if(path==='/api/schedules'){
    if(method==='GET'){
      const snapshot=clone(jobs),gate=holdList;holdList=null;
      if(gate){gate.arrived.resolve();await gate.release.promise;}
      return send(route,{jobs:snapshot});
    }
    assert.equal(method,'POST');
    const body=request.postDataJSON();
    const saved={...job('job-'+nextId++,body.name),...body};
    saved.command.argv=body.command.argv||body.command.command.split(/\s+/);
    delete saved.command.command;
    jobs.push(saved);return send(route,clone(saved),201);
  }
  const match=path.match(/^\/api\/schedules\/([^/]+)(\/run|\/runs)?$/);
  if(match){
    const [,id,action]=match;
    const item=jobs.find(item=>item.id===id);
    if(action==='/runs')return send(route,{job_id:id,log_path:'/demo/.quirq/scheduler/logs/'+id+'.log',runs:history.get(id)||[]});
    if(!item)return send(route,{detail:'No such job'},404);
    if(method==='GET'){
      if(item.running){
        polls.push({id,time:Date.now()});
        pollCounts.set(id,(pollCounts.get(id)||0)+1);
        if(pollCounts.get(id)>=2){
          item.running=false;item.last_result=result('ok');
          history.set(id,['ok','failed','timed_out','missing_binary','error','lost'].map(result));
        }
      }
      return send(route,clone(item));
    }
    if(item.running)return send(route,{detail:'Command already has a run in progress'},409);
    if(action==='/run'){
      assert.equal(method,'POST');
      item.running=true;item.running_since=new Date().toISOString();pollCounts.set(id,0);
      return send(route,{ok:true,started:true,job:clone(item)},202);
    }
    if(method==='PUT'){
      const gate=holdPut;holdPut=null;
      if(gate){gate.arrived.resolve();await gate.release.promise;}
      const body=request.postDataJSON();
      Object.assign(item,body);
      item.command.argv=body.command.argv||body.command.command.split(/\s+/);
      delete item.command.command;
      return send(route,clone(item));
    }
    assert.equal(method,'DELETE');jobs=jobs.filter(job=>job.id!==id);
    return send(route,{ok:true,deleted:id});
  }
  assert.equal(method,'GET','Unexpected mutation must never reach a real server: '+path);
  return route.continue();
});
const row=id=>page.locator('[data-command-id="'+id+'"]');
const action=(id,name)=>row(id).locator('[data-command-action="'+name+'"]');
const gate=()=>({arrived:deferred(),release:deferred()});
async function waitEnabled(selector){await page.waitForFunction(selector=>{
  const element=document.querySelector(selector);return element&&!element.disabled;
},selector);}
/* New job asks for the kind first; the rest of the form appears after. */
async function newJob(kind='once'){
  await page.locator('#command-add').click();
  await page.locator('input[name="kind"][value="'+kind+'"]').check();
}
async function commandFields(name,line='["git","status"]'){
  await page.locator('#command-name').fill(name);
  await page.locator('#command-line').fill(line);
}
async function within(selector){
  return page.locator(selector).evaluateAll(elements=>elements.filter(element=>element.getClientRects().length).every(element=>{
    const r=element.getBoundingClientRect();return r.left>=-1&&r.right<=innerWidth+1;
  }));
}

try{
  await page.goto(origin+'/space/#/setup',{waitUntil:'networkidle'});
  await page.locator('#setup-nav [data-setup-go="commands"]').click();
  await row('job-a').waitFor();
  assert.equal(await page.locator('#runtime-watcher').isChecked(),false);
  assert.equal(writes.length,0,'Mount does not seed or run anything');
  assert.equal(await page.locator('#setup-restart').isDisabled(),true);
  assert.match(await page.locator('#setup-restart-hint').textContent(),/Ctrl-C and re-run/);
  assert.equal(await page.locator('#setup-commands img').count(),0,'Description HTML is literal text');

  await page.locator('#command-add').click();
  assert.equal(await page.locator('#command-name').isVisible(),false,'New job asks for the kind first');
  await page.locator('#command-save').click();
  await page.waitForFunction(()=>document.querySelector('#command-error')?.textContent.includes('Repeating or One time'));
  await page.locator('input[name="kind"][value="once"]').check();
  assert.equal(await page.locator('#command-schedule').isVisible(),false,'A manual job has no schedule fields');
  await commandFields('Manual check');
  await page.locator('#command-description').fill('A manual fixture command');
  await page.locator('#command-timeout').fill('12');
  await page.locator('select[name="timeoutUnit"]').selectOption('seconds');
  await page.locator('#command-cwd').fill('/demo/project');
  await page.locator('#command-save').click();
  await row('job-3').waitFor();
  assert.equal(jobs.find(job=>job.id==='job-3').every_seconds,null);
  assert.equal(jobs.find(job=>job.id==='job-3').command.timeout,12);
  assert.match(await row('job-3').textContent(),/One time[\s\S]*Runs when you click Run now/);
  assert.equal(writes.some(write=>write.path.endsWith('/run')),false);

  await action('job-3','edit').click();
  assert.equal(await page.locator('input[name="kind"][value="once"]').isChecked(),true,'Edit reopens the saved kind');
  assert.equal(await page.locator('select[name="timeoutUnit"]').inputValue(),'seconds');
  await commandFields('Edited manual check','git status --short');
  await page.locator('input[name="kind"][value="scheduled"]').check();
  await page.locator('input[name="every"]').fill('1');
  await page.locator('select[name="unit"]').selectOption('minutes');
  assert.match(await page.locator('#command-schedule-preview').textContent(),/Every minute/);
  const putGate=holdPut=gate();
  await page.locator('#command-save').click();await putGate.arrived.promise;
  assert.equal(await action('job-b','edit').isDisabled(),true,'A pending save cannot replace the active form');
  await action('job-b','edit').dispatchEvent('click');
  assert.equal(await page.locator('#command-name').inputValue(),'Edited manual check');
  const listGate=holdList=gate();
  await page.locator('#setup-refresh').click();await listGate.arrived.promise;
  putGate.release.resolve();
  await page.waitForFunction(()=>document.querySelector('[data-command-id="job-3"] b')?.textContent==='Edited manual check');
  listGate.release.resolve();await waitEnabled('#setup-refresh');
  await page.waitForTimeout(150);
  assert.equal(await row('job-3').locator('b').textContent(),'Edited manual check','A stale list cannot overwrite the saved command');
  assert.deepEqual(jobs.find(job=>job.id==='job-3').command.argv,['git','status','--short']);
  assert.equal(jobs.find(job=>job.id==='job-3').every_seconds,60);
  assert.equal(jobs.find(job=>job.id==='job-3').first_run_at,null);

  await action('job-3','edit').click();
  await page.locator('input[name="repeat"][value="daily"]').check();
  await page.locator('input[name="dailyTime"]').fill('02:00');
  assert.match(await page.locator('#command-schedule-preview').textContent(),/Every day at 02:00\. First run: /);
  await page.locator('#command-save').click();
  await page.waitForFunction(()=>document.querySelector('#command-editor')?.hidden);
  const daily=jobs.find(job=>job.id==='job-3');
  assert.equal(daily.every_seconds,86400);
  assert.match(daily.first_run_at,/T02:00:00[+-]\d\d:\d\d$/,'Every day at 02:00 is a local anchor with its offset');

  jobs.find(job=>job.id==='job-b').running=true;
  await action('job-b','edit').click();await commandFields('Keep this draft');
  await page.locator('#command-save').click();
  await page.waitForFunction(()=>document.querySelector('#command-error')?.textContent.includes('in progress'));
  assert.equal(await page.locator('#command-name').inputValue(),'Keep this draft');
  await page.waitForFunction(()=>document.querySelector('[data-command-id="job-b"] [data-command-action="run"]')?.disabled);
  await page.waitForFunction(()=>document.querySelector('[data-command-id="job-b"] .is-good'),null,{timeout:10000});
  assert.equal(await page.locator('#command-name').inputValue(),'Keep this draft','Running polls preserve the edit form');
  await page.locator('#command-cancel').click();

  await action('job-a','run').click();
  await page.waitForFunction(()=>document.querySelector('[data-command-id="job-a"] .is-running'));
  await newJob();await commandFields('Draft during run');
  await page.locator('#tab-inbox').click();await page.locator('#tab-setup').click();
  await page.locator('#setup-nav [data-setup-go="commands"]').click();
  assert.equal(await page.locator('#command-name').inputValue(),'Draft during run','View refresh preserves a command draft');
  await page.waitForFunction(()=>document.querySelector('[data-command-id="job-a"] .is-good'),null,{timeout:11000});
  assert.equal(await page.locator('#command-name').inputValue(),'Draft during run');
  const ticks=polls.filter(poll=>poll.id==='job-a');
  assert.ok(ticks.length>=2&&ticks[1].time-ticks[0].time>=2800,'Running commands poll approximately every three seconds with the watcher disabled');
  assert.match(await row('job-a').textContent(),/2\.50s/);
  await page.locator('#command-cancel').click();
  await action('job-a','runs').click();
  await page.locator('.setup-run').first().waitFor();
  assert.equal(await page.locator('.setup-run').count(),6);
  assert.match(await page.locator('.setup-run pre').first().textContent(),/<img src=x/);
  assert.equal(await page.locator('#command-runs img').count(),0);
  assert.equal(await page.evaluate(()=>window.fixtureXss),undefined);
  await page.keyboard.press('1');assert.equal(new URL(page.url()).hash,'#/setup/commands','Modal keys do not switch the app');
  await page.keyboard.press('Escape');await page.locator('#command-runs').waitFor({state:'hidden'});
  await action('job-a','delete').click();await row('job-a').waitFor({state:'detached'});
  assert.equal(history.get('job-a').length,6,'Deleting the definition retains fixture history');

  for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:1000});
    await newJob('scheduled');await commandFields('Draft jobs');
    await page.locator('#setup-commands').scrollIntoViewIfNeeded();
    assert.equal(await within('#setup-commands .setup-card-head>*'),true,width+'px Jobs header');
    assert.equal(await within('#command-form input,#command-form textarea,#command-form select,#command-form button'),true,width+'px Jobs form');
    await page.waitForTimeout(500);
    await page.screenshot({path:resolve(output,'commands-'+width+'.png')});
    await page.locator('#command-cancel').click();
    await action('job-b','runs').click();await page.locator('.setup-run').first().waitFor();
    assert.equal(await within('#command-runs h2,#command-runs-close,#command-runs pre'),true,width+'px Runs drawer');
    await page.screenshot({path:resolve(output,'runs-'+width+'.png')});
    await page.locator('#command-runs-close').click();
  }

  const longName='build_'+ 'x'.repeat(58);
  jobs.find(job=>job.id==='job-b').name=longName;
  await page.locator('#setup-refresh').click();await waitEnabled('#setup-refresh');
  await row('job-b').scrollIntoViewIfNeeded();
  assert.equal(await within('[data-command-id="job-b"] b'),true,'A valid 64-character name fits the narrow Jobs row');
  await action('job-b','runs').click();await page.locator('.setup-run').first().waitFor();
  assert.equal(await within('#command-runs h2,#command-runs-close'),true,'A valid long name leaves the narrow drawer Close control reachable');
  await page.locator('#command-runs-close').click();

  await page.setViewportSize({width:1440,height:1000});
  await page.locator('#setup-nav [data-setup-go="server"]').click();
  restartMode='native';restartReject=true;
  fixtureRuntime.restart_required=false;
  fixtureRuntime.roots.change_required=false;
  await page.locator('#setup-refresh').click();await waitEnabled('#setup-restart');
  await page.locator('#setup-restart').click();
  await page.waitForFunction(()=>document.querySelector('#setup-restart-error')?.textContent.includes('already in progress'));
  fixtureRuntime.restart_required=true;
  await page.locator('#setup-refresh').click();await waitEnabled('#runtime-restart');
  await page.locator('#runtime-restart').click();
  await page.waitForFunction(()=>document.querySelector('#setup-restart-error')?.textContent.includes('already in progress'));
  await page.locator('#update-check').click();await page.locator('#update-apply').waitFor();
  await page.locator('#update-apply').click();await page.locator('#update-restart').waitFor();
  await page.locator('#update-restart').click();
  await page.waitForFunction(()=>document.querySelector('#setup-restart-error')?.textContent.includes('already in progress'));
  await waitEnabled('#update-restart');
  assert.equal(writes.filter(write=>write.path==='/space/server/restart').length,3,'Every Restart entry uses the same route');
  assert.equal(writes.some(write=>write.path==='/api/runtime-config/restart'),false);
  restartReject=false;
  const before=await page.evaluate(()=>performance.timeOrigin);
  await page.locator('#update-restart').click();
  await page.waitForTimeout(2300);
  assert.equal(await page.evaluate(()=>performance.timeOrigin),before,'A healthy response from the old instance must not reload');
  fixtureRuntime.restart_required=false;
  instanceId='fixture-after';
  await page.waitForFunction(before=>performance.timeOrigin!==before,before,{timeout:10000});
  await page.locator('#setup-nav [data-setup-go="server"]').click();
  await page.locator('#setup-restart').waitFor();
  assert.deepEqual(errors,[]);
  console.log('Jobs kind choice, plain-language schedules, CRUD, safe history, conflicts, draft/read races, watcher-independent 3s polling, desktop/mobile layouts, foreground hints and new-instance restart checks passed. Screenshots: '+output);
}catch(error){
  await page.screenshot({path:resolve(output,'failure.png')});
  throw error;
}finally{await browser.close();}
