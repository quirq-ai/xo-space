#!/usr/bin/env node
/* Manage re-entry must invalidate catalog and access reads. Fictional GETs only. */
import assert from 'node:assert/strict';
import {startDataRefresh} from './refresh-helpers.mjs';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5101',endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');assert.ok(!['5002','5112'].includes(endpoint.port));
const output=resolve(process.argv[2]||'/private/tmp/space-manage-refresh-races');await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
const page=await context.newPage();page.setDefaultTimeout(15000);
const report={checks:[],errors:[],writes:[],catalogReads:0,accessReads:0};
const gate=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
const hold=()=>({arrived:gate(),release:gate()});
const pending=[];let catalogHold=hold(),accessHold=null,name='Older catalog snapshot',members=[];
pending.push(catalogHold);
const json=(route,data)=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url());
  if(url.origin!==endpoint.origin||request.method()!=='GET'){report.writes.push(request.method()+' '+request.url());return route.abort();}
  if(url.pathname==='/api/xo-projects'){
    report.catalogReads++;const data={items:[{id:'race-project',display_name:name,description:'Fictional re-entry check'}]};
    const pause=catalogHold;catalogHold=null;if(pause){pause.arrived.resolve();await pause.release.promise;}
    return json(route,data);
  }
  if(url.pathname==='/api/xo-projects/race-project/removal'){
    report.accessReads++;const data={project_id:'race-project',can_remove:!members.length,members:structuredClone(members),peers:[],
      blockers:members.length?[{code:'shared',message:'Revoke each member before removal.'}]:[],repo:'github.com/fictional/race-project'};
    const pause=accessHold;accessHold=null;if(pause){pause.arrived.resolve();await pause.release.promise;}
    return json(route,data);
  }
  return route.continue();
});
page.on('pageerror',error=>report.errors.push(error.message));
async function go(route){await page.evaluate(route=>{location.hash='#/'+route;},route);
  await page.locator(route==='projects/manage'?'#view-project-manage.is-active':'#setup-panel-workspace').waitFor({state:'visible'});}
const checked=text=>{report.checks.push(text);console.log(text);};
try{
  const first=catalogHold;
  await page.goto(origin+'/space/#/projects/manage',{waitUntil:'domcontentloaded'});
  await page.locator('#manage-project-add').waitFor();await first.arrived.promise;
  await go('setup/workspace');name='Current catalog after re-entry';await go('projects/manage');
  first.release.resolve();await page.getByText(name,{exact:true}).waitFor();
  assert.ok(report.catalogReads>=2,'Re-entry queues a new catalog read after the old snapshot');
  assert.doesNotMatch(await page.locator('#manage-project-list').textContent(),/Older catalog snapshot/);
  checked('Leaving and returning during a pending catalog read shows a new snapshot instead of reusing the old one.');

  await page.locator('[data-project-remove="race-project"]').click();
  await page.waitForFunction(()=>!document.querySelector('#manage-project-recheck').disabled);
  await page.locator('#manage-project-confirm').fill('race-project');
  const remove=page.locator('#manage-project-delete');assert.equal(await remove.isEnabled(),true);
  const oldAccess=accessHold=hold();pending.push(oldAccess);
  await startDataRefresh(page);await oldAccess.arrived.promise;
  assert.equal(await remove.isDisabled(),true);
  await page.evaluate(()=>{window.guardViolations=[];new MutationObserver(()=>{
    const button=document.querySelector('#manage-project-delete');
    if(!button.disabled)window.guardViolations.push('enabled while revalidation was pending');
  }).observe(document.querySelector('#manage-project-delete'),{attributes:true,attributeFilter:['disabled']});});
  await go('setup/workspace');members=[{workspace_id:'new-member',role:'member',status:'active',can_revoke:true,is_self:false}];
  await go('projects/manage');assert.equal(await remove.isDisabled(),true);
  oldAccess.release.resolve();await page.locator('[data-project-revoke-start="new-member"]').waitFor();
  assert.ok(report.accessReads>=3,'Current access is rechecked after the pending snapshot');
  assert.equal(await remove.isDisabled(),true);assert.equal(await page.locator('#manage-project-confirm').inputValue(),'race-project');
  assert.deepEqual(await page.evaluate(()=>window.guardViolations),[],'Old clear authority never temporarily enables deletion');
  checked('Re-entry during an access refresh rejects the old clear result, retains typed confirmation, and keeps deletion disabled for newly shared access.');
  assert.deepEqual(report.errors,[]);assert.deepEqual(report.writes,[]);
}catch(error){report.failure=error.stack;await page.screenshot({path:resolve(output,'failure.png')}).catch(()=>{});throw error;}
finally{pending.forEach(value=>value.release.resolve());await writeFile(resolve(output,'report.json'),JSON.stringify(report,null,2));await browser.close();}
