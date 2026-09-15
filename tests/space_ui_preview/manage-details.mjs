#!/usr/bin/env node
/* Collapsible Manage details and migrated Issues use fictional reads only. */
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5101',endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');assert.ok(!['5002','5112'].includes(endpoint.port));
const captureOnly=process.argv.includes('--screenshots-only'),focusOnly=process.argv.includes('--focus-only');
const output=resolve(process.argv[2]||'/private/tmp/space-manage-details');await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
await context.addInitScript(()=>{
  window.fixtureCopies=[];window.fixtureCopyDenied=false;window.fixtureCopyPause=false;window.fixtureReleaseCopy=null;
  Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{
    if(window.fixtureCopyPause)await new Promise(done=>{window.fixtureReleaseCopy=done;});
    if(window.fixtureCopyDenied)throw new Error('Fictional clipboard denied');window.fixtureCopies.push(value);
  }}});
});
const page=await context.newPage();page.setDefaultTimeout(15000);
const report={checks:[],screenshots:[],requests:[],errors:[],writes:[]};
const gate=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
const pending=[];let heldIssue=null,issueFailure=false,emptyClosed=false;
const stamp='2026-09-14T10:00:00Z';
const catalog=[{id:'alpha',display_name:'Aurora Console',description:'Release visibility for a fictional team.',created_at:stamp,unscaffolded:false},
  {id:'beta',display_name:'Orbit API — infrastructure and release observability',description:'A fictional non-GitHub repository.',created_at:stamp,unscaffolded:true}];
const metadataFields={pid:'project-12345678-90ab-cdef-1234-567890abcdef',owner_user_id:'fictional-owner-with-a-long-workspace-identity',branch:'development/release-readiness-and-accessibility-review'};
let metadata={alpha:'git@github.com:fictional/aurora.git',beta:'https://gitlab.com/fictional/orbit.git'};
const issuePayload=(id,{stale=false}={})=>({project_id:id,state:'ok',repo:'fictional/'+id,tracked:1,fetched_at:stamp,
  issues:[{number:101,title:stale?'Stale mirror issue':'Improve keyboard navigation',state:'open',labels:['accessibility'],assignees:[{login:'demo-dev'}],updated_at:stamp,url:'https://github.com/fictional/'+id+'/issues/101'},
    {number:102,title:'Tighten mobile layout',state:'open',labels:null,assignees:[],updated_at:stamp,url:'javascript:window.issueInjected=true'},
    ...(emptyClosed?[]:[{number:90,title:'Restore keyboard focus',state:'closed',labels:['accessibility'],assignees:[],updated_at:stamp}])]});
const json=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),path=url.pathname;
  if(url.origin!==endpoint.origin||request.method()!=='GET'){report.writes.push(request.method()+' '+request.url());return route.abort();}
  report.requests.push(path+url.search);
  if(path==='/api/xo-projects')return json(route,{items:catalog});
  const detail=path.match(/^\/api\/xo-projects\/([^/]+)\/(file|github\/issues|removal|todos|activity|timeline)$/);
  if(detail){
    const [,id,kind]=detail;
    if(kind==='file')return json(route,{project_id:id,relative_path:'.xo/project.json',content:JSON.stringify(id==='alpha'?{pid:metadataFields.pid,owner_user_id:metadataFields.owner_user_id,git:{remote_url:metadata[id],default_branch:metadataFields.branch}}:{git:{remote_url:metadata[id]}}),truncated:false});
    if(kind==='github/issues'){
      if(heldIssue&&Boolean(heldIssue.force)===url.searchParams.has('refresh')){const held=heldIssue;heldIssue=null;held.arrived.resolve();await held.release.promise;return json(route,held.data||issuePayload(id,{stale:true}));}
      return json(route,issueFailure?{project_id:id,state:'error',error:'Fixture issue service unavailable',issues:[]}:issuePayload(id));
    }
    if(kind==='removal')return json(route,{project_id:id,can_remove:false,repo:'github.com/fictional/'+id,
      blockers:[{code:'shared',message:'A fictional collaborator still has access.'}],members:[{workspace_id:'fixture-member',can_revoke:true,role:'member'}],peers:[]});
    if(kind==='todos')return json(route,{project_id:id,sessions:{demo:{runtime:'local',todos:[{status:'in_progress',content:'Current '+id+' task'}]}}});
    if(kind==='activity')return json(route,{project_id:id,open_sessions:[{session_id:'session-'+id,agent:'Fixture agent',runtime:'local',opened_at:stamp,last_activity_at:stamp}]});
    if(kind==='timeline')return json(route,{events:[{project_id:id,type:'file.edited',path:'README.md',ts:stamp}],next_cursor:null});
  }
  return route.continue();
});
page.on('pageerror',error=>report.errors.push(error.message));
const row=id=>page.locator('.manage-project-row[data-project-id="'+id+'"]');
const toggle=id=>row(id).locator('.manage-project-toggle');
const details=id=>row(id).locator('.manage-project-details');
const issues=id=>details(id).locator('.iss-list');
const countIssues=()=>report.requests.filter(path=>path.includes('/github/issues')).length;
const checked=text=>{report.checks.push(text);console.log(text);};
async function go(route){await page.evaluate(route=>{location.hash='#/'+route;},route);
  await page.locator(route==='projects/manage'?'#view-project-manage.is-active':'#view-inbox-activity.is-active').waitFor({state:'visible'});}
async function screenshot(name){await page.screenshot({path:resolve(output,name),animations:'disabled'});report.screenshots.push(name);}
try{
  if(focusOnly){
    await page.goto(origin+'/space/#/projects/manage',{waitUntil:'networkidle'});await toggle('alpha').click();
    const refresh=details('alpha').locator('[data-iss-refresh]');
    for(const moveFocus of [false,true]){
      await issues('alpha').getByText('Improve keyboard navigation',{exact:true}).waitFor();
      const repoCopy=details('alpha').locator('.iss-meta [data-copy-value]');await repoCopy.focus();
      const held=heldIssue={force:true,data:{project_id:'alpha',state:'no_remote',repo:null,issues:[]},arrived:gate(),release:gate()};pending.push(held);
      await refresh.evaluate(button=>button.click());await held.arrived.promise;
      assert.equal(await refresh.isDisabled(),true);
      const owner=details('alpha').locator('[data-project-field="owner"] [data-copy-value]');
      if(moveFocus)await owner.focus();
      held.release.resolve();await issues('alpha').getByText(/No github.com remote/).waitFor();
      await page.waitForFunction(()=>!document.querySelector('[data-project-id="alpha"] [data-iss-refresh]').disabled);
      assert.equal(await (moveFocus?owner:refresh).evaluate(node=>node===document.activeElement),true,
        moveFocus?'No-remote completion preserves a newly selected control':'No-remote completion focuses Refresh after it becomes enabled');
      if(!moveFocus){await page.getByRole('tooltip').waitFor();assert.equal(await page.getByRole('tooltip').textContent(),await refresh.getAttribute('data-tip'),'Restored focus describes Refresh, not the removed repository control');}
      if(!moveFocus){await refresh.click();}
    }
    checked('A held forced refresh that removes repository controls restores enabled Refresh focus, while preserving focus moved elsewhere by the user.');
    assert.deepEqual(report.errors,[]);assert.deepEqual(report.writes,[]);
  }else if(captureOnly){
    await page.goto(origin+'/space/#/projects/manage',{waitUntil:'networkidle'});await toggle('alpha').waitFor();
    for(const state of ['collapsed','expanded']){
      if(state==='expanded'){
        await toggle('alpha').click();await issues('alpha').getByText('Improve keyboard navigation',{exact:true}).waitFor();
        await row('alpha').locator('[data-project-copy]').click();
        await row('alpha').locator('.manage-project-copy-result').getByText('GitHub URL copied.',{exact:true}).waitFor();
      }
      for(const width of [1440,390,320]){
        await page.setViewportSize({width,height:1000});await page.evaluate(()=>new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done))));
        await page.locator('#view-project-manage').evaluate(node=>{node.scrollTop=0;});
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
        await screenshot('manage-'+state+'-'+width+'.png');
      }
    }
    for(const width of [1440,390,320]){
      await page.setViewportSize({width,height:1000});
      await details('alpha').locator('.project-issues').scrollIntoViewIfNeeded();
      const issueCopy=details('alpha').locator('.iss-meta [data-copy-value]');
      await issueCopy.evaluate(node=>node.blur());
      await page.evaluate(()=>new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done))));
      await issueCopy.focus();
      await page.getByRole('tooltip').waitFor();const tipBox=await page.getByRole('tooltip').boundingBox();
      assert.ok(tipBox.x>=0&&tipBox.x+tipBox.width<=width,'Copy tooltip stays within the '+width+'px viewport');
      await issueCopy.press('Escape');await page.getByRole('tooltip').waitFor({state:'hidden'});
      await screenshot('manage-issues-'+width+'.png');
    }
    await row('alpha').locator('[data-project-activity]').click();await page.waitForURL('**/#/inbox/activity');
    await page.locator('[data-activity-todo-rows]').getByText('Current alpha task',{exact:true}).waitFor();
    for(const width of [1440,390,320]){
      await page.setViewportSize({width,height:1000});await page.evaluate(()=>new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done))));
      await page.locator('#view-inbox-activity').evaluate(node=>{node.scrollTop=0;});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      await screenshot('project-activity-'+width+'.png');
    }
    checked('Default collapsed Manage, expanded Issues with copy success, and selected-project Activity fit desktop and phone widths.');
    assert.deepEqual(report.errors,[]);assert.deepEqual(report.writes,[]);
  }else{
  await page.goto(origin+'/space/#/projects/manage',{waitUntil:'networkidle'});await toggle('alpha').waitFor();
  for(const id of ['alpha','beta']){
    assert.equal(await toggle(id).getAttribute('aria-expanded'),'false');assert.equal(await details(id).isVisible(),false);
  }
  assert.equal(countIssues(),0,'Collapsed cards do not load issue mirrors');
  const pin=row('alpha').locator('[data-project-pin="alpha"]');
  const pinNode=await pin.elementHandle(),cardNode=await row('alpha').elementHandle();
  const order=await page.locator('.manage-project-row').evaluateAll(nodes=>nodes.map(node=>node.dataset.projectId));
  await pin.focus();await pin.press('Space');
  assert.equal(await pin.getAttribute('aria-pressed'),'true');
  assert.equal(await pin.evaluate(node=>node===document.activeElement),true);
  assert.equal(await toggle('alpha').getAttribute('aria-expanded'),'false');assert.equal(countIssues(),0);
  assert.equal(await cardNode.evaluate(node=>node===document.querySelector('.manage-project-row[data-project-id="alpha"]')),true);
  assert.deepEqual(await page.locator('.manage-project-row').evaluateAll(nodes=>nodes.map(node=>node.dataset.projectId)),order);
  await page.locator('#project-refresh').click();await page.waitForFunction(()=>!document.querySelector('#project-refresh').disabled);
  assert.equal(await pinNode.evaluate(node=>node===document.querySelector('[data-project-pin="alpha"]')),true);
  await page.reload({waitUntil:'networkidle'});await pin.waitFor();
  assert.equal(await pin.getAttribute('aria-pressed'),'true','Manage pins persist across reload');
  const key=await page.evaluate(()=>Object.keys(localStorage).find(key=>key.startsWith('space.projects.pins.v1:')));
  assert.ok(key,'Pin storage keeps its existing workspace-scoped key');
  await context.route('**/__pins_peer__',route=>route.fulfill({contentType:'text/html',body:'<!doctype html><title>Fictional pin storage peer</title>'}));
  const peer=await context.newPage();await peer.goto(origin+'/__pins_peer__');
  await peer.evaluate(key=>localStorage.setItem(key,'[]'),key);
  await page.waitForFunction(()=>document.querySelector('[data-project-pin="alpha"]').getAttribute('aria-pressed')==='false');
  await peer.evaluate(key=>localStorage.setItem(key,'["alpha"]'),key);
  await page.waitForFunction(()=>document.querySelector('[data-project-pin="alpha"]').getAttribute('aria-pressed')==='true');
  await peer.close();
  await page.evaluate(key=>{window.fixtureOriginalSetItem=Storage.prototype.setItem;Storage.prototype.setItem=function(name,value){
    if(name===key)throw new DOMException('Fictional quota','QuotaExceededError');return window.fixtureOriginalSetItem.call(this,name,value);};},key);
  await pin.click();assert.equal(await pin.getAttribute('aria-pressed'),'false');
  assert.match(await row('alpha').locator('.manage-project-pin-notice').textContent(),/could not|couldn't|cannot|not saved|this tab|session only/i);
  assert.equal(await page.evaluate(key=>localStorage.getItem(key),key),'["alpha"]','Failed persistence does not pretend storage changed');
  await page.evaluate(()=>{Storage.prototype.setItem=window.fixtureOriginalSetItem;});await pin.click();
  assert.equal(await pin.getAttribute('aria-pressed'),'true');
  assert.equal(await row('alpha').locator('.manage-project-pin-notice').isVisible(),false);
  assert.equal(await toggle('alpha').getAttribute('aria-expanded'),'false');assert.equal(countIssues(),0);
  checked('Collapsed Manage pin actions retain focus and card order, persist across reload, sync cross-tab storage, and report failed persistence without losing current-tab state.');

  await row('alpha').locator('[data-project-copy]').click();
  await page.waitForFunction(()=>window.fixtureCopies.length===1);
  assert.deepEqual(await page.evaluate(()=>window.fixtureCopies),['https://github.com/fictional/aurora']);
  assert.equal(await toggle('alpha').getAttribute('aria-expanded'),'false');assert.equal(countIssues(),0);
  await row('beta').locator('[data-project-copy]').click();
  await row('beta').locator('.manage-project-copy-result').getByText('No GitHub URL is recorded for this project.',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>window.fixtureCopies.length),1,'Non-GitHub remotes are never copied');
  metadata.beta='https://token@github.com/fictional/orbit.git';await row('beta').locator('[data-project-copy]').click();
  await page.waitForFunction(()=>!document.querySelector('[data-project-copy="beta"]').disabled);
  assert.equal(await page.evaluate(()=>window.fixtureCopies.length),1,'Credential-bearing remotes are never copied');
  assert.equal(await toggle('beta').getAttribute('aria-expanded'),'false');
  checked('Manage cards start closed; Copy GitHub URL accepts a GitHub SSH remote without opening details or fetching Issues.');

  const held=heldIssue={arrived:gate(),release:gate()};pending.push(held);
  await toggle('alpha').focus();await toggle('alpha').press('Enter');await held.arrived.promise;
  assert.equal(await toggle('alpha').getAttribute('aria-expanded'),'true');
  assert.equal(await toggle('alpha').evaluate(node=>node===document.activeElement),true);
  await details('alpha').locator('[data-iss-refresh]').click();
  await issues('alpha').getByText('Improve keyboard navigation',{exact:true}).waitFor();
  held.release.resolve();await page.waitForLoadState('networkidle');
  assert.equal(await issues('alpha').getByText('Stale mirror issue').count(),0);
  assert.equal(report.requests.filter(path=>path.endsWith('/github/issues?refresh=1')).length,1);
  const unsafe=issues('alpha').getByText('Tighten mobile layout').locator('..');
  assert.equal(await unsafe.evaluate(node=>node.tagName==='A'),false,'Unsafe issue URLs remain text');
  checked('Keyboard expansion retains focus and loads Issues lazily; a newer forced refresh wins over an older mirror read.');

  const query=details('alpha').locator('.iss-q');const input=await query.elementHandle();
  await details('alpha').locator('[data-iss-state="all"]').click();await query.fill('keyboard');
  await page.waitForFunction(()=>document.querySelectorAll('[data-project-id="alpha"] .iss-row').length===2);
  const reads=countIssues(),polls=report.requests.filter(path=>path.endsWith('/github/issues?refresh=1')).length;await toggle('alpha').focus();await toggle('alpha').press('Space');
  assert.equal(await toggle('alpha').getAttribute('aria-expanded'),'false');
  await toggle('alpha').press('Enter');assert.equal(await query.inputValue(),'keyboard');
  assert.equal(countIssues(),reads,'Local filters and collapse do not read again');
  await go('inbox/activity');await go('projects/manage');
  assert.equal(await query.inputValue(),'keyboard');assert.equal(await details('alpha').locator('[data-iss-state="all"]').getAttribute('aria-pressed'),'true');
  assert.equal(await input.evaluate(node=>node.isConnected&&node===document.querySelector('[data-project-id="alpha"] .iss-q')),true);
  assert.equal(report.requests.filter(path=>path.endsWith('/github/issues?refresh=1')).length,polls,'Re-entry may refresh the mirror without polling GitHub');
  await toggle('beta').focus();await toggle('beta').press('Enter');
  assert.equal(await toggle('alpha').getAttribute('aria-expanded'),'false');
  assert.equal(await toggle('beta').getAttribute('aria-expanded'),'true');
  assert.equal(await page.locator('.manage-project-details:visible').count(),1);
  await page.waitForLoadState('networkidle');
  assert.equal(await details('beta').locator('[data-project-field="github"] [data-copy-value]').count(),0,'An unavailable GitHub value has no metadata copy control');
  assert.equal(await toggle('beta').evaluate(node=>node===document.activeElement),true);
  await toggle('alpha').focus();await toggle('alpha').press('Space');
  assert.equal(await toggle('beta').getAttribute('aria-expanded'),'false');
  assert.equal(await page.locator('.manage-project-details:visible').count(),1);
  assert.equal(await query.inputValue(),'keyboard');
  assert.equal(await input.evaluate(node=>node.isConnected),true,'Closing another card retains its issue controls');
  checked('Manage is a single-open accordion; keyboard activation retains focus and closed cards keep their own issue state.');
  await query.fill('');await details('alpha').locator('[data-iss-state="closed"]').click();
  await issues('alpha').getByText('Restore keyboard focus',{exact:true}).waitFor();
  emptyClosed=true;await details('alpha').locator('[data-iss-refresh]').click();
  await issues('alpha').getByText(/No closed issues recorded/).waitFor();
  assert.match(await issues('alpha').textContent(),/watches it close/);
  issueFailure=true;await details('alpha').locator('[data-iss-refresh]').click();
  await details('alpha').getByText(/Last poll failed|unavailable/).waitFor();
  issueFailure=false;emptyClosed=false;await details('alpha').locator('[data-iss-refresh]').click();
  await issues('alpha').getByText('Restore keyboard focus',{exact:true}).waitFor();
  checked('Issues retain filter controls through collapse/navigation, explain recorded closed history, and recover from explicit-refresh errors.');

  const values={name:catalog[0].display_name,description:catalog[0].description,id:'alpha',created:stamp,
    uuid:metadataFields.pid,owner:metadataFields.owner_user_id,branch:metadataFields.branch,github:'https://github.com/fictional/aurora'};
  for(const [field,value] of Object.entries(values)){
    const button=details('alpha').locator('[data-project-field="'+field+'"] [data-copy-value]');
    await button.waitFor();assert.equal(await button.getAttribute('data-copy-value'),value,field+' has the exact source value');
    const label=await button.getAttribute('aria-label');assert.ok(label?.startsWith('Copy '),field+' has an accessible action label');
    assert.equal(await button.getAttribute('data-tip'),label,field+' has a named tooltip');
    assert.equal(await button.locator('[data-icon="copy"]').count(),1,field+' uses the shared copy icon');
    assert.equal(await button.locator('[data-icon="check"]').count(),1,field+' includes success feedback without rebuilding the button');
    assert.equal(await button.textContent(),'');
    const before=await page.evaluate(()=>window.fixtureCopies.length);
    await button.focus();
    await page.getByRole('tooltip').waitFor();assert.equal(await page.getByRole('tooltip').textContent(),label);
    assert.equal(await button.getAttribute('aria-describedby'),await page.getByRole('tooltip').getAttribute('id'));
    await button.press('Escape');await page.getByRole('tooltip').waitFor({state:'hidden'});
    assert.equal(await button.evaluate(node=>node===document.activeElement),true,'Escape dismisses only the tooltip');
    await button.press('Enter');
    await page.waitForFunction(count=>window.fixtureCopies.length===count,before+1);
    assert.equal(await page.evaluate(()=>window.fixtureCopies.at(-1)),value);
    assert.equal(await button.evaluate(node=>node===document.activeElement),true,'Keyboard copy keeps focus for '+field);
  }
  assert.equal(await details('alpha').locator('[data-project-field="metadata"] [data-copy-value]').count(),0,'Derived availability is not a copied source value');
  for(const field of ['name','id']){
    const button=row('alpha').locator('[data-project-copy-field="'+field+'"]');
    assert.equal(await button.getAttribute('data-copy-value'),values[field]);
    assert.equal(await button.getAttribute('data-tip'),await button.getAttribute('aria-label'));
    const before=await page.evaluate(()=>window.fixtureCopies.length);await button.focus();await button.press('Enter');
    await page.waitForFunction(count=>window.fixtureCopies.length===count,before+1);
    assert.equal(await page.evaluate(()=>window.fixtureCopies.at(-1)),values[field]);
    assert.equal(await toggle('alpha').getAttribute('aria-expanded'),'true','Header copies do not collapse a card');
  }
  await page.locator('#view-project-manage').evaluate(node=>{node.scrollTop=0;});
  const cardBox=await row('alpha').boundingBox();
  await page.mouse.click(cardBox.x+cardBox.width-8,cardBox.y+8);
  assert.equal(await toggle('alpha').getAttribute('aria-expanded'),'false','Blank card header space is an expansion target');
  await toggle('alpha').click();
  await details('alpha').locator('[data-project-field="name"] .manage-project-value').click();
  assert.equal(await toggle('alpha').getAttribute('aria-expanded'),'true','Clicking detail text does not collapse its card');
  const repoCopy=details('alpha').locator('.iss-meta [data-copy-value]');
  const beforeRepo=await page.evaluate(()=>window.fixtureCopies.length);await repoCopy.focus();await repoCopy.press('Enter');
  await page.waitForFunction(count=>window.fixtureCopies.length===count,beforeRepo+1);
  assert.equal(await page.evaluate(()=>window.fixtureCopies.at(-1)),'fictional/alpha');
  const repoCopyNode=await repoCopy.elementHandle();
  await page.locator('#project-refresh').evaluate(button=>button.click());
  await page.waitForFunction(()=>!document.querySelector('#project-refresh').disabled);
  assert.equal(await repoCopyNode.evaluate(node=>node.isConnected&&node===document.activeElement),true,'Refreshing counts and time retains focused issue-repository copy control');
  const owner=details('alpha').locator('[data-project-field="owner"] [data-copy-value]');
  const ownerNode=await owner.elementHandle();await owner.focus();
  await page.locator('#project-refresh').evaluate(button=>button.click());
  await page.waitForFunction(()=>!document.querySelector('#project-refresh').disabled);
  assert.equal(await ownerNode.evaluate(node=>node.isConnected&&node===document.activeElement),true,'Metadata refresh retains the same focused copy button');
  const beforeDelayedCopy=await page.evaluate(()=>window.fixtureCopies.length),oldOwner=metadataFields.owner_user_id;
  await page.evaluate(()=>{window.fixtureCopyPause=true;window.fixtureReleaseCopy=null;});await owner.press('Enter');
  await page.waitForFunction(()=>typeof window.fixtureReleaseCopy==='function');
  metadataFields.owner_user_id='fictional-owner-updated-after-refresh';
  await page.locator('#project-refresh').evaluate(button=>button.click());
  await page.waitForFunction(()=>!document.querySelector('#project-refresh').disabled);
  assert.equal(await ownerNode.evaluate(node=>node.isConnected&&node===document.activeElement),true,'Changed metadata updates the value without replacing the focused button');
  assert.equal(await owner.getAttribute('data-copy-value'),metadataFields.owner_user_id);
  await page.evaluate(()=>{window.fixtureCopyPause=false;window.fixtureReleaseCopy();});
  await page.waitForFunction(count=>window.fixtureCopies.length===count,beforeDelayedCopy+1);
  assert.equal(await page.evaluate(()=>window.fixtureCopies.at(-1)),oldOwner,'The original clipboard operation copied its original value');
  assert.notEqual(await owner.getAttribute('data-copy-state'),'done','Late clipboard completion cannot label the replacement value Copied');
  assert.equal(await owner.getAttribute('data-tip'),await owner.getAttribute('aria-label'));
  const beforeCopyFailure=await page.evaluate(()=>window.fixtureCopies.length);
  await page.evaluate(()=>{window.fixtureCopyDenied=true;});await owner.press('Enter');
  await page.waitForFunction(()=>document.querySelector('[data-project-field="owner"] [data-copy-value]').dataset.copyState==='error');
  assert.equal(await page.evaluate(()=>window.fixtureCopies.length),beforeCopyFailure);
  assert.match(await page.locator('#manage-projects > .project-copy-live').textContent(),/Could not copy/);
  assert.equal(await owner.evaluate(node=>node===document.activeElement),true);
  await page.evaluate(()=>{window.fixtureCopyDenied=false;});await owner.press('Enter');
  await page.waitForFunction(count=>window.fixtureCopies.length===count,beforeCopyFailure+1);
  assert.equal(await page.evaluate(()=>window.fixtureCopies.at(-1)),metadataFields.owner_user_id);
  const savedOwner=metadataFields.owner_user_id;delete metadataFields.owner_user_id;
  await owner.focus();await page.getByRole('tooltip').waitFor();
  await page.locator('#project-refresh').evaluate(button=>button.click());
  await page.waitForFunction(()=>!document.querySelector('#project-refresh').disabled);
  assert.equal(await ownerNode.evaluate(node=>node.isConnected),false);
  assert.equal(await toggle('alpha').evaluate(node=>node===document.activeElement),true,'Removing focused metadata returns focus to its card');
  await page.getByRole('tooltip').waitFor({state:'hidden'});
  metadataFields.owner_user_id=savedOwner;
  await page.locator('#project-refresh').evaluate(button=>button.click());await page.waitForFunction(()=>!document.querySelector('#project-refresh').disabled);
  checked('Exact metadata copies, tooltips and focused controls survive refresh; stale clipboard feedback and removed-field tooltips are suppressed.');

  await toggle('alpha').click();
  await row('alpha').locator('[data-project-share]').click();
  const share=row('alpha').locator('.project-share');await share.locator('[name=workspace_id]').fill('fictional-recipient');
  assert.equal(await toggle('alpha').getAttribute('aria-expanded'),'false');
  await toggle('beta').click();assert.equal(await share.isVisible(),true,'Opening another card does not close an inline share draft');
  assert.equal(await share.locator('[name=workspace_id]').inputValue(),'fictional-recipient');
  await toggle('alpha').click();await toggle('alpha').click();assert.equal(await share.locator('[name=workspace_id]').inputValue(),'fictional-recipient');
  await row('alpha').locator('[data-project-remove]').click();await page.locator('#manage-project-removal').waitFor();
  assert.equal(await toggle('alpha').getAttribute('aria-expanded'),'false');assert.equal(await page.locator('#manage-project-delete').isDisabled(),true);
  await page.locator('#manage-project-close').click();await share.locator('[data-share-cancel]').click();
  const copiesBeforeDenied=await page.evaluate(()=>window.fixtureCopies.length);
  await page.evaluate(()=>{window.fixtureCopyDenied=true;});await row('alpha').locator('[data-project-copy]').click();
  await row('alpha').locator('.manage-project-copy-result').getByText(/Could not copy the URL/).waitFor();
  assert.equal(await page.evaluate(()=>window.fixtureCopies.length),copiesBeforeDenied);
  assert.equal(await row('alpha').locator('[data-project-copy]').isEnabled(),true,'Clipboard failure leaves retry available');
  assert.equal(await toggle('alpha').getAttribute('aria-expanded'),'false');
  checked('Share, Remove and Copy stay independent of card expansion, preserve inline drafts, and perform no accidental project mutations.');

  await go('inbox/activity');await page.locator('#view-search').fill('older unrelated query');
  await go('projects/manage');assert.equal(await toggle('alpha').getAttribute('aria-expanded'),'false');
  assert.equal(await row('alpha').locator('[data-project-activity]').isVisible(),true);
  await row('alpha').locator('[data-project-activity]').focus();await row('alpha').locator('[data-project-activity]').press('Enter');
  await page.waitForURL('**/#/inbox/activity');
  await page.waitForFunction(()=>document.querySelector('[data-activity-project-filter]').value==='alpha');
  assert.equal(await page.locator('#view-search').inputValue(),'');
  await page.locator('[data-activity-todo-rows]').getByText('Current alpha task',{exact:true}).waitFor();
  assert.match(await page.locator('[data-activity-live-rows]').textContent(),/Fixture agent/);
  checked('Collapsed header View activity opens the selected project in Inbox, clears older event search, and displays its todos and current sessions.');

  await go('projects/manage');assert.equal(await toggle('alpha').getAttribute('aria-expanded'),'false','Activity does not expand the source card');
  await toggle('alpha').click();await details('alpha').locator('[data-iss-state="all"]').click();
  for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:1000});await page.evaluate(()=>new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done))));
    await page.locator('#view-project-manage').evaluate(node=>{node.scrollTop=0;});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    const outside=await page.locator('#view-project-manage button:visible,#view-project-manage input:visible').evaluateAll(nodes=>nodes.filter(node=>{
      const b=node.getBoundingClientRect();return b.left< -1||b.right>innerWidth+1;
    }).map(node=>node.className));assert.deepEqual(outside,[]);
    await screenshot('manage-details-'+width+'.png');
  }
  checked('Expanded Manage cards, issue filters and independent actions fit desktop, 390px and 320px screens.');
  assert.deepEqual(report.errors,[]);assert.deepEqual(report.writes,[]);
  }
}catch(error){report.failure=error.stack;report.ui=await page.evaluate(()=>({width:innerWidth,active:{tag:document.activeElement?.tagName,classes:document.activeElement?.className,tip:document.activeElement?.dataset?.tip},tooltips:[...document.querySelectorAll('[role=tooltip]')].map(node=>({hidden:node.hidden,text:node.textContent}))})).catch(()=>null);await page.screenshot({path:resolve(output,'failure.png')}).catch(()=>{});throw error;}
finally{pending.forEach(item=>item.release.resolve());await writeFile(resolve(output,'report.json'),JSON.stringify(report,null,2));await browser.close();}
