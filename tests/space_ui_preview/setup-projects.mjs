/* Project management uses fictional browser fixtures exclusively. Every clone,
   revoke, roster change and deletion is intercepted before reaching a server. */
import assert from 'node:assert/strict';
import {openProjectList,openProjectPage} from './routes.mjs';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5100';
const endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');
assert.notEqual(endpoint.port,'5002');
assert.notEqual(endpoint.port,'5112','Preserve the interactive Commands server');
const output=resolve(process.argv[2]||'/tmp/space-setup-projects');
await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE
  ?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const browser=await chromium.launch({headless:true,
  executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
const page=await context.newPage();
const report={origin,checks:[],requests:[],writes:[],errors:[],screenshots:[],pageLoads:[]};
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
const gate=()=>({arrived:deferred(),release:deferred()});
const owner={workspace_id:'fixture-own-space',role:'owner',status:'active',can_revoke:false,is_self:true};
const members=new Map([['shared-demo',[
  {...owner},{workspace_id:'alice-space',role:'member',status:'active',can_revoke:true,is_self:false},
  {workspace_id:'bob-space',role:'member',status:'active',can_revoke:true,is_self:false}]],
  ['incoming-demo',[{workspace_id:'external-owner',role:'owner',status:'active',can_revoke:false,is_self:false}]]]);
const peers=new Map([['shared-demo',[{user_id:'charlie-user',label:'Charlie <local>',role:'member'}]]]);
const catalog=['solo-demo','shared-demo','unknown-demo','incoming-demo'].map(id=>({id,display_name:id==='shared-demo'?'Shared <demo>':id,description:'A fictional project',unscaffolded:false}));
const faults=new Map();
let holdClone=null,holdDelete=null,cloneError=null,deleteConflict=false;
const holds=new Map();
function preflight(id){
  const ms=members.get(id)||[],ps=peers.get(id)||[],blockers=[];
  if(ms.some(member=>member.can_revoke))blockers.push({code:'shared',message:'Revoke access for each user before removing this project.'});
  if(ps.length)blockers.push({code:'peers',message:'Remove each collaborator from the local roster.'});
  if(id==='incoming-demo')blockers.push({code:'not_owner',message:'Only the project owner can revoke access. Ask the owner to remove access first.'});
  return{project_id:id,can_remove:!blockers.length,blockers,members:ms,peers:ps,repo:ms.length?'github.com/fixture/'+id:null};
}
const send=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
async function pause(pending){if(pending){pending.arrived.resolve();await pending.release.promise;}}
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),path=url.pathname,method=request.method();
  report.requests.push({path,method});
  if(url.origin!==endpoint.origin){report.errors.push('Blocked external request '+request.url());return route.abort();}
  if(path==='/api/project-sharing/status'&&method==='GET')return send(route,{
    cadence:'active',last_poll_at:'2026-09-14T10:00:00Z',last_poll_ok:true,watch_branch:'main',
    own_workspace_id:'fixture-own-space',projects_root:'/fictional/projects',recent:[],
    repos:{'github.com/fixture/removed-project':{project:null,shared:true,available:true,members:1,
      auto_clone_suppressed:true,clone:{state:'removed',detail:'Removed from this Space'}}},
  });
  if(path==='/api/xo-projects'){
    if(method==='GET')return send(route,{items:catalog,total:catalog.length});
    if(method==='POST'){
      const body=request.postDataJSON();report.writes.push({path,method,body});
      assert.deepEqual(Object.keys(body).sort(),['project_id','repository_url']);
      const pending=holdClone;holdClone=null;await pause(pending);
      if(cloneError)return send(route,{detail:cloneError},409);
      catalog.push({id:body.project_id,display_name:body.project_id,description:'Cloned fixture',unscaffolded:true});
      return send(route,{project_id:body.project_id,created:true,warning:'Project added; automatic restore remains paused.'});
    }
  }
  const removal=path.match(/^\/api\/xo-projects\/([^/]+)\/removal$/);
  if(removal&&method==='GET'){
    const id=decodeURIComponent(removal[1]),fault=faults.get(id),snapshot=structuredClone(fault?.data??preflight(id));
    const pending=holds.get(id);holds.delete(id);await pause(pending);
    return send(route,snapshot,fault?.status||200);
  }
  const revoke=path.match(/^\/api\/xo-projects\/([^/]+)\/revoke$/);
  if(revoke&&method==='POST'){
    const id=decodeURIComponent(revoke[1]),body=request.postDataJSON();report.writes.push({path,method,body});
    const member=members.get(id)?.find(member=>member.workspace_id===body.workspace_id);
    assert.equal(member?.can_revoke,true);member.can_revoke=false;member.status='revoked';
    return send(route,{ok:true});
  }
  const peer=path.match(/^\/api\/xo-projects\/([^/]+)\/peers\/([^/]+)$/);
  if(peer&&method==='DELETE'){
    const id=decodeURIComponent(peer[1]),user=decodeURIComponent(peer[2]);report.writes.push({path,method});
    peers.set(id,(peers.get(id)||[]).filter(peer=>peer.user_id!==user));return send(route,{removed:true});
  }
  const project=path.match(/^\/api\/xo-projects\/([^/]+)$/);
  if(project&&method==='DELETE'){
    const id=decodeURIComponent(project[1]),body=request.postDataJSON();report.writes.push({path,method,body});
    assert.deepEqual(body,{confirm_project_id:id});
    const pending=holdDelete;holdDelete=null;await pause(pending);
    if(deleteConflict){members.set(id,[{...owner},{workspace_id:'newly-shared-user',role:'member',status:'active',can_revoke:true,is_self:false}]);return send(route,{detail:'Access changed. Revoke access for every user before removal.'},409);}
    assert.equal(preflight(id).can_remove,true,'Fixture refuses deletion with remaining access');
    const index=catalog.findIndex(project=>project.id===id);assert.notEqual(index,-1);catalog.splice(index,1);
    return send(route,{project_id:id,removed:true});
  }
  if(method!=='GET'){
    report.errors.push('Blocked unexpected write '+method+' '+path);return send(route,{detail:'Fixture blocked unexpected write'},403);
  }
  return route.continue();
});
page.on('pageerror',error=>report.errors.push(error.message));
page.on('request',request=>{if(request.isNavigationRequest()&&request.frame()===page.mainFrame())report.pageLoads.push(request.url());});
page.on('console',message=>{
  if(message.type()!=='error')return;
  const url=message.location().url;
  if(url.startsWith(origin+'/api/xo-projects')&&/\b(?:409|503)\b/.test(message.text()))return;
  report.errors.push(message.text());
});
const projectRoot=page.locator('#view-project-manage'),removeButton=page.locator('#manage-project-delete');
const open=id=>projectRoot.locator('[data-project-remove="'+id+'"]');
const totalDeletes=()=>report.writes.filter(write=>write.method==='DELETE'&&!write.path.includes('/peers/')).length;
const checked=text=>{report.checks.push(text);console.log(text);};
async function chooseProjects(){await openProjectPage(page,'manage');await projectRoot.waitFor();await open('solo-demo').waitFor();}
async function openRemove(id){await open(id).click();await page.waitForFunction(id=>document.querySelector('#manage-project-removal-id')?.textContent===id&&!document.querySelector('#manage-project-recheck')?.disabled,id);}
async function close(){await page.locator('#manage-project-close').click();}
async function screenshot(name){await projectRoot.scrollIntoViewIfNeeded();await page.screenshot({path:resolve(output,name)});report.screenshots.push(name);}

try{
  await page.goto(origin+'/space/#/setup',{waitUntil:'networkidle'});
  assert.equal(report.requests.some(request=>request.path.endsWith('/removal')),false);
  await chooseProjects();assert.equal(await projectRoot.locator('.manage-project-row').count(),4);
  assert.match(await projectRoot.textContent(),/Shared <demo>/);assert.equal(await projectRoot.locator('img,script').count(),0);
  assert.deepEqual(report.writes,[]);
  checked('Manage lists local projects; opening it never changes files, users or sharing.');

  await page.locator('#manage-project-add').click();
  await page.locator('#manage-project-repository').fill('https://github.com/fixture/example-project.git');
  assert.equal(await page.locator('#manage-project-id').inputValue(),'example-project');
  await page.locator('#manage-project-id').fill('my-demo');
  assert.doesNotMatch(await page.locator('#setup-step-intelligence').textContent(),/Unsaved changes/,'Clone drafts do not mark Intelligence as unsaved');
  await page.locator('#manage-project-repository').fill('git@github.com:fixture/changed-name.git');
  assert.equal(await page.locator('#manage-project-id').inputValue(),'my-demo');
  const repositoryNode=await page.locator('#manage-project-repository').elementHandle();
  await page.locator('#tab-setup').click();
  await page.locator('#setup-nav [data-setup-go="intelligence"]').click();
  assert.equal(await page.locator('#setup-nav [data-setup-go="projects"]').count(),0,'Setup no longer owns project management');
  await chooseProjects();
  assert.equal(await repositoryNode.evaluate(node=>node.isConnected),true,'Manage keeps the clone draft mounted across Setup visits');
  await page.locator('#project-refresh').click();
  await page.locator('#tab-setup').click();
  await openProjectList(page);await page.waitForURL('**/#/projects/data/list');
  await page.locator('#prj-row-solo-demo').waitFor();
  assert.doesNotMatch(await page.locator('body').textContent(),/a workspace knowledge graph/i);
  await page.locator('#view-search').fill('demo');
  // Boot the graph before the catalog changes so later checks exercise the
  // existing atlas rather than a fresh map loaded after the mutation.
  await openProjectPage(page,'graph');
  await page.waitForFunction(()=>document.querySelector('#q')?.placeholder.match(/Search \d+/));
  assert.equal(await page.locator('.atlas-project-refresh').count(),0);
  await page.locator('#tab-setup').click();await chooseProjects();
  assert.equal(await repositoryNode.evaluate(node=>node.isConnected),true);
  assert.equal(await page.locator('#manage-project-repository').inputValue(),'git@github.com:fixture/changed-name.git');
  assert.equal(await page.locator('#manage-project-id').inputValue(),'my-demo');
  cloneError='A folder with that name already exists.';await page.locator('#manage-project-create').click();
  await page.locator('#manage-project-add-error').waitFor();assert.equal(await page.locator('#manage-project-id').inputValue(),'my-demo');
  cloneError=null;const cloning=holdClone=gate();await page.locator('#manage-project-create').click();await cloning.arrived.promise;
  assert.equal(await page.locator('#manage-project-create').isDisabled(),true);
  assert.equal(await page.locator('#manage-project-cancel').isDisabled(),true);
  cloning.release.resolve();await open('my-demo').waitFor();
  assert.equal(await page.locator('#manage-project-form').isVisible(),false);
  assert.equal(await page.locator('#manage-project-repository').inputValue(),'','A confirmed clone clears its draft');
  assert.equal(await page.locator('#manage-project-notice').textContent(),'Project added; automatic restore remains paused.');
  assert.equal(report.writes.filter(write=>write.method==='POST'&&write.path==='/api/xo-projects').length,2);
  await openProjectList(page);await page.locator('#prj-row-my-demo').waitFor();
  assert.equal(await page.locator('#view-search').inputValue(),'demo','A changed catalog preserves the Projects filter');
  await page.locator('#tab-setup').click();await chooseProjects();
  checked('Clone URL suggests a folder name until edited; drafts survive refresh/navigation, errors preserve input, and a pending clone cannot duplicate.');

  await openRemove('shared-demo');
  assert.equal(await page.locator('#manage-project-removal').isVisible(),true,'Manage owns the removal review');
  assert.doesNotMatch(await page.locator('#setup-step-intelligence').textContent(),/Unsaved changes/);
  assert.equal(await removeButton.isDisabled(),true);
  assert.equal(await projectRoot.locator('[data-project-revoke-start]').count(),2);
  assert.equal(await projectRoot.locator('[data-project-peer-start]').count(),1);
  assert.match(await page.locator('#manage-project-access').textContent(),/Charlie <local>/);
  assert.equal(totalDeletes(),0);
  await projectRoot.locator('[data-project-revoke-start="alice-space"]').click();
  assert.equal(report.writes.filter(write=>write.path.endsWith('/revoke')).length,0,'Revoke requires its own confirmation');
  await projectRoot.locator('[data-project-revoke-cancel]').click();
  for(const user of ['alice-space','bob-space']){
    await projectRoot.locator('[data-project-revoke-start="'+user+'"]').click();
    await projectRoot.locator('[data-project-revoke="'+user+'"]').click();
    await page.waitForFunction(()=>!document.querySelector('#manage-project-recheck').disabled);
    assert.equal(await removeButton.isDisabled(),true,'Every remaining user or roster member blocks local deletion');
  }
  const revokes=report.writes.filter(write=>write.path.endsWith('/revoke'));
  assert.deepEqual(revokes.map(write=>write.body),[{workspace_id:'alice-space'},{workspace_id:'bob-space'}]);
  await projectRoot.locator('[data-project-peer-start="charlie-user"]').click();
  assert.equal(report.writes.some(write=>write.path.includes('/peers/')),false);
  await projectRoot.locator('[data-project-peer="charlie-user"]').click();
  await page.waitForFunction(()=>!document.querySelector('#manage-project-recheck').disabled);
  assert.match(await page.locator('#manage-project-access').textContent(),/Access checked/);
  assert.match(await page.locator('#manage-project-access').textContent(),/Revoked/);
  assert.equal(await removeButton.isDisabled(),true,'Typing the project id is still required');
  await page.locator('#manage-project-confirm').fill('shared-demo-wrong');assert.equal(await removeButton.isDisabled(),true);
  await page.locator('#manage-project-confirm').fill('shared-demo');assert.equal(await removeButton.isEnabled(),true);
  await screenshot('projects-shared-cleared-1440.png');
  const deleting=holdDelete=gate();await removeButton.click();await deleting.arrived.promise;
  assert.equal(await removeButton.isDisabled(),true);assert.equal(await page.locator('#manage-project-close').isDisabled(),true);
  assert.equal(await open('solo-demo').isDisabled(),true);deleting.release.resolve();
  await page.waitForFunction(()=>!document.querySelector('[data-project-remove="shared-demo"]'));
  assert.equal(totalDeletes(),1);
  await openProjectList(page);
  await page.waitForFunction(()=>!document.querySelector('#prj-row-shared-demo'));
  assert.equal(await page.locator('#view-search').inputValue(),'demo');
  await page.locator('#tab-setup').click();await chooseProjects();
  checked('Shared project removal requires each separate revoke and local-roster confirmation, fresh access verification, and the exact project id.');
  checked('The tagline is absent; an already mounted Projects list refreshes additions/removals while keeping its filter.');

  await openRemove('incoming-demo');assert.equal(await removeButton.isDisabled(),true);
  assert.match(await page.locator('#manage-project-access').textContent(),/Only the project owner/);
  assert.equal(await projectRoot.locator('[data-project-revoke-start]').count(),0);
  await close();
  faults.set('unknown-demo',{status:503,data:{detail:'Sharing is unavailable. Check again before removing this project.'}});
  await openRemove('unknown-demo');await page.locator('#manage-project-confirm').fill('unknown-demo');
  assert.equal(await removeButton.isDisabled(),true);
  faults.set('unknown-demo',{data:{project_id:'unknown-demo',can_remove:true,blockers:[]}});
  await page.locator('#manage-project-recheck').click();await page.waitForFunction(()=>!document.querySelector('#manage-project-recheck').disabled);
  assert.equal(await removeButton.isDisabled(),true);assert.match(await page.locator('#manage-project-access').textContent(),/could not be verified/);
  faults.delete('unknown-demo');await close();
  checked('Incoming-owner restrictions, failed status reads and incomplete success responses all keep deletion disabled.');

  const stale=gate();holds.set('unknown-demo',stale);await open('unknown-demo').click();await stale.arrived.promise;
  await open('incoming-demo').click();stale.release.resolve();
  await page.waitForFunction(()=>document.querySelector('#manage-project-access')?.textContent.includes('Only the project owner'));
  assert.equal(await page.locator('#manage-project-removal-id').textContent(),'incoming-demo');
  assert.equal(await removeButton.isDisabled(),true);
  await close();
  await openRemove('solo-demo');await page.locator('#manage-project-confirm').fill('solo-demo');
  const before=gate();holds.set('solo-demo',before);await page.locator('#manage-project-recheck').click();await before.arrived.promise;
  assert.equal(await removeButton.isDisabled(),true);
  members.set('solo-demo',[{...owner},{workspace_id:'arrived-during-check',role:'member',status:'active',can_revoke:true,is_self:false}]);
  const after=gate();holds.set('solo-demo',after);await page.locator('#project-refresh').click();
  before.release.resolve();await after.arrived.promise;
  assert.equal(await removeButton.isDisabled(),true);after.release.resolve();
  await projectRoot.locator('[data-project-revoke-start="arrived-during-check"]').waitFor();
  assert.equal(await removeButton.isDisabled(),true);
  assert.equal(await page.locator('#manage-project-confirm').inputValue(),'solo-demo','Refresh keeps the typed confirmation but rechecks its authority');
  members.delete('solo-demo');await close();
  checked('Late status for another project is ignored; a queued recheck cannot enable deletion from a stale clear result.');

  await openRemove('solo-demo');await page.locator('#manage-project-confirm').fill('solo-demo');
  deleteConflict=true;await removeButton.click();
  await projectRoot.locator('[data-project-revoke-start="newly-shared-user"]').waitFor();
  assert.equal(await removeButton.isDisabled(),true);assert.equal(await open('solo-demo').count(),1);
  assert.match(await page.locator('#manage-project-remove-error').textContent(),/Access changed/);
  assert.equal(totalDeletes(),2,'A rejected server deletion is not automatically retried');
  deleteConflict=false;
  checked('Server rejection after an access change preserves the project, shows the new member and requires new explicit action.');

  for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:1000});await screenshot('projects-access-'+width+'.png');
    const bounds=await projectRoot.locator('input,button,b,code,p').evaluateAll(nodes=>nodes.filter(node=>node.getClientRects().length).map(node=>{const r=node.getBoundingClientRect();return{left:r.left,right:r.right};}));
    assert.ok(bounds.every(bound=>bound.left>=-1&&bound.right<=width+1),width+'px project content fits');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  }
  checked('Project list, access review and confirmation controls fit desktop and narrow screens.');

  await page.setViewportSize({width:1440,height:1000});await close();
  await page.locator('#manage-project-add').click();
  await page.locator('#manage-project-repository').fill('https://github.com/fixture/keep-this-draft.git');
  await page.locator('#manage-project-id').fill('unsaved-project-folder');
  const draftNode=await page.locator('#manage-project-repository').elementHandle();
  const writesBefore=report.writes.length,loadsBefore=report.pageLoads.length;
  await openProjectList(page);await page.waitForLoadState('networkidle');
  const graphReads=report.requests.filter(request=>request.path==='/xo/space.json').length;
  for(const lens of ['graph','time']){
    await openProjectPage(page,lens);
    await page.locator('#view-'+lens+'.is-active').waitFor();
    await page.waitForLoadState('networkidle');
    if(lens==='graph')await page.waitForFunction(()=>!document.querySelector('#q').disabled);
    const notice=page.locator('#view-'+lens+' .atlas-project-refresh');
    if(await notice.isVisible()){
      await notice.getByRole('button',{name:'Refresh map',exact:true}).click();
      await notice.waitFor({state:'detached'});
      await page.waitForLoadState('networkidle');
    }
    assert.equal(await notice.count(),0,'The rebuilt projection has fresh data and no stale-data notice');
  }
  assert.ok(report.requests.filter(request=>request.path==='/xo/space.json').length>graphReads,
    'Catalog mutations invalidate the cached graph dataset before revisiting it');
  assert.equal(report.pageLoads.length,loadsBefore,'Refreshing projections never reloads the document');
  await page.locator('#tab-setup').click();await chooseProjects();
  assert.equal(await draftNode.evaluate(node=>node.isConnected),true);
  assert.equal(await page.locator('#manage-project-repository').inputValue(),'https://github.com/fixture/keep-this-draft.git');
  assert.equal(await page.locator('#manage-project-id').inputValue(),'unsaved-project-folder');
  checked('Catalog mutations refresh Graph and Timeline data without reloading the document or losing the Manage draft.');

  await openProjectPage(page,'sharing');
  const suppressed=page.locator('.shl-inbox-row').filter({hasText:'github.com/fixture/removed-project'});await suppressed.waitFor();
  assert.match(await suppressed.textContent(),/removed locally/);
  assert.match(await suppressed.textContent(),/automatic cloning is paused/);
  assert.doesNotMatch(await suppressed.textContent(),/clone failed|XO Space clones it on the next check/);
  await suppressed.getByRole('button',{name:'Clone project',exact:true}).click();
  await page.locator('#view-project-manage.is-active').waitFor();
  assert.equal(new URL(page.url()).hash,'#/projects/manage');
  assert.equal(await page.locator('#section-nav [data-section-page="project-manage"]').getAttribute('aria-current'),'page');
  assert.equal(await draftNode.evaluate(node=>node.isConnected),true);
  assert.equal(await page.locator('#manage-project-repository').inputValue(),'https://github.com/fixture/keep-this-draft.git');
  assert.equal(await page.locator('#manage-project-id').inputValue(),'unsaved-project-folder');
  assert.equal(report.writes.length,writesBefore,'Viewing a suppressed project and opening Manage never clones or changes access');
  assert.equal(report.pageLoads.length,loadsBefore);
  checked('A suppressed Sharing entry says removed locally; Clone project opens Manage and preserves its draft without initiating a clone.');
  assert.deepEqual(report.errors,[]);
}catch(error){report.failure=error.stack;await page.screenshot({path:resolve(output,'failure.png')}).catch(()=>{});throw error;}
finally{await writeFile(resolve(output,'checks.json'),JSON.stringify(report,null,2)+'\n');await browser.close();}
