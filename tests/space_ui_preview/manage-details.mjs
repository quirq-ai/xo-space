#!/usr/bin/env node
/* Collapsible Manage details and migrated Issues use fictional reads only. */
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5101',endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');assert.ok(!['5002','5112'].includes(endpoint.port));
const captureOnly=process.argv.includes('--screenshots-only');
const output=resolve(process.argv[2]||'/private/tmp/space-manage-details');await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
await context.addInitScript(()=>{
  window.fixtureCopies=[];window.fixtureCopyDenied=false;
  Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{
    if(window.fixtureCopyDenied)throw new Error('Fictional clipboard denied');window.fixtureCopies.push(value);
  }}});
});
const page=await context.newPage();page.setDefaultTimeout(15000);
const report={checks:[],screenshots:[],requests:[],errors:[],writes:[]};
const gate=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
const pending=[];let heldIssue=null,issueFailure=false,emptyClosed=false;
const stamp='2026-09-14T10:00:00Z';
const catalog=[{id:'alpha',display_name:'Aurora Console',description:'Release visibility for a fictional team.'},
  {id:'beta',display_name:'Orbit API',description:'A fictional non-GitHub repository.'}];
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
    if(kind==='file')return json(route,{project_id:id,relative_path:'.xo/project.json',content:JSON.stringify({git:{remote_url:metadata[id]}}),truncated:false});
    if(kind==='github/issues'){
      if(heldIssue&&!url.searchParams.has('refresh')){const held=heldIssue;heldIssue=null;held.arrived.resolve();await held.release.promise;return json(route,issuePayload(id,{stale:true}));}
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
  if(captureOnly){
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

  await toggle('alpha').click();
  await row('alpha').locator('[data-project-share]').click();
  const share=row('alpha').locator('.project-share');await share.locator('[name=workspace_id]').fill('fictional-recipient');
  assert.equal(await toggle('alpha').getAttribute('aria-expanded'),'false');
  await toggle('alpha').click();await toggle('alpha').click();assert.equal(await share.locator('[name=workspace_id]').inputValue(),'fictional-recipient');
  await row('alpha').locator('[data-project-remove]').click();await page.locator('#manage-project-removal').waitFor();
  assert.equal(await toggle('alpha').getAttribute('aria-expanded'),'false');assert.equal(await page.locator('#manage-project-delete').isDisabled(),true);
  await page.locator('#manage-project-close').click();await share.locator('[data-share-cancel]').click();
  await page.evaluate(()=>{window.fixtureCopyDenied=true;});await row('alpha').locator('[data-project-copy]').click();
  await row('alpha').locator('.manage-project-copy-result').getByText(/Could not copy the URL/).waitFor();
  assert.equal(await page.evaluate(()=>window.fixtureCopies.length),1);
  assert.equal(await row('alpha').locator('[data-project-copy]').isEnabled(),true,'Clipboard failure leaves retry available');
  assert.equal(await toggle('alpha').getAttribute('aria-expanded'),'false');
  checked('Share, Remove and Copy stay independent of card expansion, preserve inline drafts, and perform no accidental project mutations.');

  await go('inbox/activity');await page.locator('#view-search').fill('older unrelated query');
  await go('projects/manage');await toggle('alpha').click();
  await row('alpha').locator('[data-project-activity]').click();
  await page.waitForURL('**/#/inbox/activity');
  await page.waitForFunction(()=>document.querySelector('[data-activity-project-filter]').value==='alpha');
  assert.equal(await page.locator('#view-search').inputValue(),'');
  await page.locator('[data-activity-todo-rows]').getByText('Current alpha task',{exact:true}).waitFor();
  assert.match(await page.locator('[data-activity-live-rows]').textContent(),/Fixture agent/);
  checked('View activity opens the selected project in Inbox, clears older event search, and displays its todos and current sessions.');

  await go('projects/manage');await details('alpha').locator('[data-iss-state="all"]').click();
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
}catch(error){report.failure=error.stack;await page.screenshot({path:resolve(output,'failure.png')}).catch(()=>{});throw error;}
finally{pending.forEach(item=>item.release.resolve());await writeFile(resolve(output,'report.json'),JSON.stringify(report,null,2));await browser.close();}
