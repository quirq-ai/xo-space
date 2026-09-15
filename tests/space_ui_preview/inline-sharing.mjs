#!/usr/bin/env node
/* Actual UI with in-memory share responses. No service mutation may pass. */
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {openProjectList} from './routes.mjs';
const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5101',endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');assert.ok(!['5002','5112'].includes(endpoint.port));
const output=resolve(process.argv[2]||'/private/tmp/space-inline-sharing');await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
const page=await context.newPage();page.setDefaultTimeout(15000);
const report={checks:[],screenshots:[],errors:[],blockedWrites:[],mockedShares:[]};
const gate=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
const holds=[];
let nextResponse={status:200,data:{ok:true,repo:'github.com/fictional/aurora-console'}},pending=null;
const json=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url());
  if(url.origin!==endpoint.origin){report.blockedWrites.push(request.method()+' '+request.url());return route.abort();}
  if(request.method()==='POST'&&/^\/api\/xo-projects\/[^/]+\/share$/.test(url.pathname)){
    report.mockedShares.push({path:url.pathname,body:request.postDataJSON()});
    const response=structuredClone(nextResponse),hold=pending;pending=null;
    if(hold){hold.arrived.resolve();await hold.release.promise;}
    return json(route,response.data,response.status);
  }
  if(request.method()!=='GET'){report.blockedWrites.push(request.method()+' '+url.pathname);return route.abort();}
  return route.continue();
});
page.on('pageerror',error=>report.errors.push(error.message));
page.on('requestfailed',request=>{if(/\.(?:js|css)(?:\?|$)/.test(request.url()))report.errors.push(request.url()+': '+request.failure()?.errorText);});
page.on('console',message=>{
  if(message.type()!=='error')return;
  if(/\/api\/xo-projects\/[^/]+\/share/.test(message.location().url)&&/503/.test(message.text()))return;
  report.errors.push(message.text());
});
await page.addInitScript(()=>{window.inlineSharingDocument='same-document';window.sharedAccessEvents=[];
  addEventListener('space:project-access-changed',event=>window.sharedAccessEvents.push(event.detail));});
const checked=text=>{report.checks.push(text);console.log(text);};
const formFor=(_scope,id='aurora-console')=>page.locator('#view-project-manage form.project-share[data-project-id="'+id+'"]');
const triggerFor=(_scope,id='aurora-console')=>page.locator('[data-project-share="'+id+'"]');
async function go(scope){
  await page.evaluate(()=>{location.hash='#/projects/manage';});await page.locator('#view-project-manage').waitFor({state:'visible'});await triggerFor(scope).waitFor();
}
function routeFor(){return '#/projects/manage';}
async function assertInline(scope){
  assert.equal(new URL(page.url()).hash,routeFor(scope));
  assert.equal(await page.evaluate(()=>window.inlineSharingDocument),'same-document');
}
async function open(scope,id='aurora-console'){
  await triggerFor(scope,id).click();await formFor(scope,id).waitFor({state:'visible'});
  await assertInline(scope);return formFor(scope,id);
}
async function refresh(scope){
  await page.locator('#project-refresh').click();
  await page.waitForFunction(()=>!document.querySelector('#project-refresh').disabled);
}
try{
  await page.goto(origin+'/space/#/projects/data/list',{waitUntil:'networkidle'});
  for(const scope of ['manage']){
    await go(scope);const form=await open(scope),input=form.locator('input'),submit=form.locator('[data-share-submit]'),cancel=form.locator('[data-share-cancel]');
    const before=report.mockedShares.length;
    assert.equal(await input.evaluate(node=>node===document.activeElement),true,scope+' opens with Space ID focused');
    await submit.click();assert.equal(await input.getAttribute('aria-invalid'),'true');
    await input.fill('invalid space id');await input.press('Enter');assert.equal(report.mockedShares.length,before);
    await input.fill('recipient-'+scope+'-draft');const node=await input.elementHandle();
    await refresh(scope);assert.equal(await node.evaluate(element=>element.isConnected),true);
    assert.equal(await input.inputValue(),'recipient-'+scope+'-draft');
    await page.locator('#manage-project-add').click();await page.locator('#manage-project-repository').fill('https://github.com/fictional/keep-clone-draft.git');
    await page.evaluate(()=>{location.hash='#/setup/workspace';});await page.locator('#setup-panel-workspace').waitFor({state:'visible'});await go(scope);
    assert.equal(await page.locator('#manage-project-repository').inputValue(),'https://github.com/fictional/keep-clone-draft.git');
    assert.equal(await node.evaluate(element=>element===document.querySelector(element.tagName.toLowerCase()+'#'+element.id)),true);
    assert.equal(await input.inputValue(),'recipient-'+scope+'-draft');await assertInline(scope);
    await cancel.click();await form.waitFor({state:'hidden'});
    assert.equal(await triggerFor(scope).evaluate(element=>element===document.activeElement),true);
    assert.equal(report.mockedShares.length,before,'Opening, editing, refresh and cancellation never grant access');
    checked(scope+': inline Space ID form validates locally, keeps drafts through refresh/navigation, and Cancel restores the row action.');

    await open(scope);await input.fill('  recipient-'+scope+'  ');
    nextResponse={status:503,data:{detail:{code:'fixture_recipient',message:'Fictional recipient is unavailable.'}}};
    pending={arrived:gate(),release:gate()};const hold=pending;holds.push(hold);
    await input.press('Enter');await hold.arrived.promise;
    assert.equal(await input.isDisabled(),true);assert.equal(await submit.isDisabled(),true);assert.equal(await cancel.isDisabled(),true);
    await form.evaluate(element=>element.requestSubmit());assert.equal(report.mockedShares.length,before+1);
    await openProjectList(page);assert.equal(await page.locator('#view-projects .project-share,#view-projects .prj-share').count(),0,'Data has no sharing controls');
    await go(scope);assert.equal(await submit.isDisabled(),true,'A pending grant remains locked after returning to Manage');
    await form.evaluate(element=>element.requestSubmit());assert.equal(report.mockedShares.length,before+1);
    await go(scope);hold.release.resolve();await form.locator('[data-share-result][data-state="error"]').waitFor();
    assert.equal(await submit.isDisabled(),false);assert.equal(await input.inputValue(),'  recipient-'+scope+'  ');
    assert.equal((await page.evaluate(()=>window.sharedAccessEvents)).length,0,'Failure emits no access-change event');
    nextResponse={status:200,data:{ok:true,repo:'github.com/fictional/aurora-console'}};
    await submit.click();await form.locator('[data-share-result][data-state="success"]').waitFor();
    assert.match(await form.locator('[data-share-result]').textContent(),new RegExp('recipient-'+scope));
    assert.equal(await submit.isDisabled(),true,'A confirmed identical recipient is not submitted twice');
    assert.deepEqual(report.mockedShares.at(-1),{path:'/api/xo-projects/aurora-console/share',body:{workspace_id:'recipient-'+scope}});
    await assertInline(scope);
    checked(scope+': mocked submission trims the Space ID, locks duplicate requests across navigation, preserves input after errors, and confirms success inline.');
    await cancel.click();
  }
  for(const scope of ['manage']){
    await go(scope);const form=await open(scope);await form.locator('input').fill('recipient-space-id-for-preview');
    for(const width of [1440,390,320]){
      await page.setViewportSize({width,height:1000});await form.scrollIntoViewIfNeeded();
      const bounds=await form.locator('input,button').evaluateAll(nodes=>nodes.map(node=>{const r=node.getBoundingClientRect();return{left:r.left,right:r.right};}));
      assert.ok(bounds.every(bound=>bound.left>=-1&&bound.right<=width+1),scope+' form controls fit '+width);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      const name=scope+'-inline-share-'+width+'.png';await page.screenshot({path:resolve(output,name),animations:'disabled'});report.screenshots.push(name);
    }
    await form.locator('[data-share-cancel]').click();
  }
  checked('Manage inline sharing fits desktop, 390px and 320px screens without page overflow.');
  assert.deepEqual(report.errors,[]);assert.deepEqual(report.blockedWrites,[]);
}catch(error){report.failure=error.stack;await page.screenshot({path:resolve(output,'failure.png')}).catch(()=>{});throw error;}
finally{for(const hold of holds)hold.release.resolve();pending?.release.resolve();await writeFile(resolve(output,'report.json'),JSON.stringify(report,null,2));await browser.close();}
