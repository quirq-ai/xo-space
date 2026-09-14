/* Real browser → scheduler API → executor → history, on commands_server.py only.
   This creates and manually runs a benign Python print in disposable state.
   No process-control route or user workspace is touched. */
import assert from 'node:assert/strict';
import {mkdir} from 'node:fs/promises';
import {resolve,basename} from 'node:path';
import {pathToFileURL} from 'node:url';

const origin=process.env.SPACE_COMMANDS_URL||'http://127.0.0.1:5112';
const endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');
assert.notEqual(endpoint.port,'5002','Never target a workspace server');
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE
  ?pathToFileURL(process.env.PLAYWRIGHT_MODULE).href:'playwright');
const browser=await chromium.launch({headless:true,
  executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000}});
const page=await context.newPage();
const output=resolve(process.argv[2]||'/tmp/space-commands-review');
await mkdir(output,{recursive:true});
const errors=[],writes=[],reads=[];
page.on('pageerror',error=>errors.push(error.message));
page.on('dialog',dialog=>dialog.accept());
const fixtureResponse=await context.request.get(origin+'/__fixture__/runtime');
assert.equal(fixtureResponse.status(),200,'Requires the isolated commands_server.py');
const fixture=await fixtureResponse.json();
assert.equal(fixture.automatic_jobs,false);
assert.match(basename(fixture.cwd),/^space-command-review-/);
assert.ok(fixture.python.startsWith('/'));
const argv=[fixture.python,'-c','print("Isolated browser command completed. <result>ok</result>")'];
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url());
  assert.equal(url.origin,endpoint.origin,'No external services');
  if(request.method()==='GET')reads.push({path:url.pathname,time:Date.now()});
  else{
    assert.match(url.pathname,/^\/api\/schedules(?:\/[^/]+(?:\/run)?)?$/,'No process-control writes');
    writes.push({path:url.pathname,method:request.method()});
  }
  await route.continue();
});
let createdId=null;
try{
  await page.goto(origin+'/space/#/secrets',{waitUntil:'networkidle'});
  await page.locator('#setup-nav [data-setup-go="commands"]').click();
  await page.locator('#command-add').click();
  await page.locator('#command-name').fill('Verify isolated command execution');
  await page.locator('#command-description').fill('Print a fixture result with automatic jobs disabled.');
  await page.locator('#command-line').fill(JSON.stringify(argv));
  await page.locator('#command-cwd').fill(fixture.cwd);
  await page.locator('#command-timeout').fill('5');
  const createdResponse=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/schedules'&&response.request().method()==='POST');
  await page.locator('#command-save').click();
  const response=await createdResponse;
  assert.equal(response.status(),201);
  const job=await response.json(),id=job.id;
  createdId=id;
  assert.deepEqual(job.command.argv,argv);
  assert.equal(job.command.cwd,fixture.cwd);
  assert.equal(job.every_seconds,null);
  assert.equal(job.last_result,null,'Saving a command does not execute it');
  const row=page.locator(`[data-command-id="${id}"]`);
  await row.waitFor();
  const action=name=>row.locator(`[data-command-action="${name}"]`);
  const startedAt=Date.now();
  const runResponse=page.waitForResponse(response=>new URL(response.url()).pathname===`/api/schedules/${id}/run`);
  await action('run').click();
  assert.equal((await runResponse).status(),202);
  await row.locator('.is-good').waitFor({timeout:12000});
  const poll=reads.find(read=>read.path===`/api/schedules/${id}`&&read.time>=startedAt);
  assert.ok(poll&&poll.time-startedAt>=2800,'The UI harvests the result with its 3s running poll');
  assert.match(await row.textContent(),/ok · .* · [0-9.]+s/);
  assert.match(await row.locator('.setup-command-preview').textContent(),/Isolated browser command completed/);
  assert.equal((await action('runs').textContent()).trim(),'Inbox');
  await row.scrollIntoViewIfNeeded();
  await page.screenshot({path:resolve(output,'commands-live-result.png')});
  await action('runs').click();
  await page.locator('.setup-run').waitFor();
  assert.equal(await page.locator('.setup-run').count(),1);
  assert.match(await page.locator('.setup-run').textContent(),/manual · exit 0/);
  assert.equal(await page.locator('.setup-run pre').textContent(),'Isolated browser command completed. <result>ok</result>\n');
  assert.equal(await page.locator('.setup-run result').count(),0,'Actual command output stays text');
  await page.screenshot({path:resolve(output,'commands-live-history.png')});
  const history=await (await context.request.get(`${origin}/api/schedules/${id}/runs`)).json();
  assert.equal(history.runs.length,1);
  assert.equal(history.runs[0].status,'ok');
  assert.ok(history.log_path.startsWith(fixture.cwd+'/'));
  assert.deepEqual(writes.map(write=>write.method),['POST','POST']);
  await page.locator('#command-runs-close').click();
  await action('edit').click();
  await page.locator('#command-interval').fill('60');
  const saved=page.waitForResponse(response=>new URL(response.url()).pathname===`/api/schedules/${id}`&&response.request().method()==='PUT');
  await page.locator('#command-save').click();
  assert.equal((await saved).status(),200);
  await page.locator('#tab-inbox').click();
  const scheduled=page.locator(`[data-job="${id}"][data-act="job-results"]`);
  await scheduled.waitFor();
  await scheduled.click();
  await page.locator('.setup-run').waitFor();
  assert.match(await page.locator('.setup-run pre').textContent(),/Isolated browser command completed/);
  await page.locator('#command-runs-close').click();
  assert.equal((await context.request.delete(`${origin}/api/schedules/${id}`)).status(),200);
  createdId=null;
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,jobId:id,status:history.runs[0].status,
    pollDelayMs:poll.time-startedAt,automaticJobs:false,output},null,2));
}finally{
  if(createdId)await context.request.delete(`${origin}/api/schedules/${createdId}`).catch(()=>{});
  await browser.close();
}
