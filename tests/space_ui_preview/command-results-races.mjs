/* Actual drawer/API modules with delayed browser-only responses. No commands,
   Inbox items, or server processes are changed. Start the read-only fixture
   server.py first; SPACE_PREVIEW_URL selects its isolated loopback port. */
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';

const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5100';
const endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');
assert.notEqual(endpoint.port,'5002','Never target the workspace server');
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE
  ?pathToFileURL(process.env.PLAYWRIGHT_MODULE).href:'playwright');
const browser=await chromium.launch({headless:true,
  executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext();
const page=await context.newPage(),errors=[],reads=[];
page.on('pageerror',error=>errors.push(error.message));
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
const gate=()=>({arrived:deferred(),release:deferred(),responded:deferred()});
const jobs=new Map(),histories=new Map(),statusGates=new Map(),historyGates=new Map();
const result=label=>({status:'ok',started_at:'2026-09-14T10:00:00Z',finished_at:'2026-09-14T10:00:01Z',
  duration_seconds:1,trigger:'manual',returncode:0,
  output_tail:label+' <img src=x onerror="window.resultsXss=true"> & literal output'});
function seed(id,running=false){
  jobs.set(id,{id,name:id,command:{argv:['fixture-check'],cwd:'/demo/workspace'},
    running,running_since:running?'2026-09-14T10:00:00Z':null,last_result:null});
  histories.set(id,[]);
}
function finish(id,label){
  const record=result(label);
  Object.assign(jobs.get(id),{running:false,running_since:null,last_result:record});
  histories.set(id,[record]);
}
for(const id of ['ordered','active','old','other','reopened'])seed(id);
const send=(route,data)=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url());
  assert.equal(url.origin,endpoint.origin,'No external services');
  assert.equal(request.method(),'GET','Opening results cannot mutate data');
  if(url.pathname==='/space/__command-results-races__'){
    return route.fulfill({status:200,contentType:'text/html',body:`<!doctype html><html><body>
      ${[...jobs.keys()].map(id=>`<div data-command-id="${id}"><button id="open-${id}" data-command-action="runs">${id}</button></div>`).join('')}
      <div id="toast"></div><script type="module">
      import {openCommandResults} from '/space/js/core/command-results.js';
      for(const button of document.querySelectorAll('[data-command-action]')){
        button.addEventListener('click',()=>openCommandResults({id:button.closest('[data-command-id]').dataset.commandId}));
      }
      document.body.dataset.ready='true';
      </script></body></html>`});
  }
  const match=url.pathname.match(/^\/api\/schedules\/([^/]+)(\/runs)?$/);
  if(match){
    const [,id,history]=match;
    assert.ok(jobs.has(id),'Only fictional job IDs are read');
    reads.push({id,history:!!history,url:url.href});
    const gates=history?historyGates:statusGates,pending=gates.get(id);
    gates.delete(id);
    // History snapshots are taken on arrival. A held status is completed by
    // the test before responding, reproducing the original parallel-read race.
    const snapshot=history?{job_id:id,log_path:'/demo/logs/'+id+'.log',runs:structuredClone(histories.get(id))}:null;
    if(pending){pending.arrived.resolve();await pending.release.promise;}
    await send(route,history?snapshot:structuredClone(jobs.get(id)));
    pending?.responded.resolve();
    return;
  }
  assert.ok(!url.pathname.startsWith('/api/'),'Unexpected API read: '+url.pathname);
  await route.continue();
});
const open=id=>page.locator('#open-'+id).click();
async function close(){
  await page.evaluate(()=>new Promise(resolve=>{
    document.querySelector('#command-runs').addEventListener('close',resolve,{once:true});
    document.querySelector('#command-runs-close').click();
  }));
}
async function output(label){
  await page.waitForFunction(label=>document.querySelector('.setup-run pre')?.textContent.includes(label),label);
  assert.equal(await page.locator('.setup-run pre').textContent(),result(label).output_tail);
  assert.equal(await page.locator('#command-runs img').count(),0,'Output is text, never executable HTML');
  assert.equal(await page.evaluate(()=>window.resultsXss),undefined);
}
async function release(pending){
  pending.release.resolve();await pending.responded.promise;
  // Let the fulfilled response settle in the browser before checking stale UI.
  await page.waitForTimeout(100);
}

try{
  await page.goto(origin+'/space/__command-results-races__');
  await page.waitForFunction(()=>document.body.dataset.ready==='true');

  const ordered=gate();statusGates.set('ordered',ordered);
  await open('ordered');await ordered.arrived.promise;
  await page.waitForTimeout(100);
  assert.equal(reads.some(read=>read.id==='ordered'&&read.history),false,
    'History waits for status, so completion cannot leave a pre-completion snapshot');
  finish('ordered','Completed while status was delayed');
  await release(ordered);await output('Completed while status was delayed');
  await close();

  jobs.get('active').running=true;
  await open('active');
  await page.getByText('This command is running. Its result will appear here when it finishes.').waitFor();
  finish('active','Automatic terminal output');
  await output('Automatic terminal output');
  assert.equal(reads.filter(read=>read.id==='active'&&!read.history).length,2,
    'The running poll delivers terminal output without Refresh');
  await close();

  const oldStatus=gate();statusGates.set('old',oldStatus);
  await open('old');await oldStatus.arrived.promise;
  await close();finish('other','Other command output');await open('other');
  await output('Other command output');await release(oldStatus);
  assert.equal(reads.some(read=>read.id==='old'&&read.history),false,
    'A status response from a closed drawer cannot start another read');
  await output('Other command output');await close();
  assert.equal(await page.locator('#open-other').evaluate(el=>el===document.activeElement),true);

  jobs.get('reopened').running=true;
  const oldHistory=gate();historyGates.set('reopened',oldHistory);
  await open('reopened');await oldHistory.arrived.promise;
  await close();finish('reopened','Fresh after reopening');await open('reopened');
  await output('Fresh after reopening');
  const historyReads=reads.filter(read=>read.id==='reopened'&&read.history);
  assert.equal(historyReads.length,2,'Reopening must not share a still-pending history snapshot');
  assert.notEqual(historyReads[0].url,historyReads[1].url);
  await release(oldHistory);await output('Fresh after reopening');await close();

  // The same invalidation guard also protects another command from old history.
  const otherHistory=gate();historyGates.set('old',otherHistory);
  await open('old');await otherHistory.arrived.promise;
  await close();await open('other');await output('Other command output');
  await release(otherHistory);await output('Other command output');await close();
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,checks:[
    'status-before-history','active-to-terminal','close-during-status',
    'fresh-history-after-reopen','different-job-stale-history','escaped-output','focus-restored'
  ]},null,2));
}finally{
  for(const pending of [...statusGates.values(),...historyGates.values()])pending.release.resolve();
  await browser.close();
}
