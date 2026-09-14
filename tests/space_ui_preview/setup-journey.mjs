/* Guided Setup regression. All setting/credential/restart writes are handled
   in browser memory; this never changes an installation or executes commands. */
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5100';
const endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');
assert.notEqual(endpoint.port,'5002');
const output=resolve(process.argv[2]||'/tmp/space-setup-journey-review');
await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE
  ?pathToFileURL(process.env.PLAYWRIGHT_MODULE).href:'playwright');
const browser=await chromium.launch({headless:true,
  executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000}});
const page=await context.newPage(),errors=[],writes=[];
page.on('pageerror',error=>errors.push(error.message));
page.on('dialog',dialog=>dialog.accept());
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve};};
const gate=()=>({arrived:deferred(),release:deferred()});
let holdRead=null,holdSecret=null,failRuntime=false;
const fixture=await (await context.request.get(origin+'/api/runtime-config')).json();
fixture.configured={agent_name:'claude_code',watcher_enabled:true,watcher_interval_seconds:1,watcher_source_mode:'all'};
fixture.applied=structuredClone(fixture.configured);
fixture.roots.configured={xo_projects_root:'/demo/projects',quirq_state_root:'/demo/.quirq'};
fixture.roots.applied=structuredClone(fixture.roots.configured);
fixture.roots.apply_command='quirq install --projects-root /demo/next-projects';
fixture.agents=fixture.agents.slice(0,3).map(agent=>({...agent,secrets:[{
  key:'DEMO_API_KEY',label:'Demo API key',description:'Fictional provider credential for browser testing.',
}]}));
fixture.managed_container=false;
let secretRevision=0,restartMode='native';
let secrets=[{key:'DEMO_STORED_TOKEN',is_set:true}];
function status(){
  const value=structuredClone(fixture);
  const changed=JSON.stringify(value.configured)!==JSON.stringify(value.applied);
  value.roots.change_required=JSON.stringify(value.roots.configured)!==JSON.stringify(value.roots.applied);
  value.restart_required=changed||secretRevision>0||value.roots.change_required;
  value.restart_reasons=[...(changed?['runtime']:[]),...(secretRevision?['secrets']:[])];
  value.restart_supported=restartMode!=='foreground';value.restart_mode=restartMode;
  for(const agent of value.agents)agent.active=agent.name===value.applied.agent_name;
  return value;
}
const send=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),path=url.pathname,method=request.method();
  assert.equal(url.origin,endpoint.origin,'No external services');
  if(method!=='GET')writes.push({path,method,body:request.postDataJSON()});
  if(path==='/api/runtime-config'){
    if(method==='GET'){
      const snapshot=status(),pending=holdRead;holdRead=null;
      if(pending){pending.arrived.resolve();await pending.release.promise;}
      return send(route,failRuntime?{detail:'Fixture unavailable'}:snapshot,failRuntime?503:200);
    }
    assert.equal(method,'PUT');
    const body=request.postDataJSON();
    assert.deepEqual(Object.keys(body).sort(),['agent_name','watcher_enabled','watcher_interval_seconds','watcher_source_mode']);
    fixture.configured=structuredClone(body);
    return send(route,{ok:true,saved:body,status:status()});
  }
  if(path==='/api/runtime-config/roots'){
    assert.equal(method,'PUT');const body=request.postDataJSON();
    assert.deepEqual(Object.keys(body).sort(),['quirq_state_root','xo_projects_root']);
    fixture.roots.configured=structuredClone(body);
    return send(route,{ok:true,saved:body,status:status()});
  }
  if(path==='/api/secrets'){assert.equal(method,'GET');return send(route,{items:secrets});}
  if(path.startsWith('/api/secrets/')){
    const key=decodeURIComponent(path.slice('/api/secrets/'.length));
    if(method==='PATCH'){
      const pending=holdSecret;holdSecret=null;if(pending){pending.arrived.resolve();await pending.release.promise;}
      secrets=secrets.filter(item=>item.key!==key).concat({key,is_set:true});secretRevision++;
      return send(route,{ok:true,key,is_set:true});
    }
    assert.equal(method,'DELETE');secrets=secrets.filter(item=>item.key!==key);secretRevision++;
    return send(route,{ok:true,deleted:true});
  }
  if(path==='/api/schedules'){assert.equal(method,'GET');return send(route,{jobs:[]});}
  if(path==='/space/server/status')return send(route,{status:'on',instance_id:'setup-fixture',restart_mode:restartMode});
  if(path==='/space/server/restart'){
    assert.equal(method,'POST');return send(route,{detail:'Fixture restart already in progress'},409);
  }
  assert.equal(method,'GET','Unexpected writes never reach the fixture server');
  await route.continue();
});
const panel=id=>page.locator('#setup-panel-'+id);
async function choose(id){
  await page.locator(`#setup-nav [data-setup-go="${id}"]`).click();
  await panel(id).waitFor();
  assert.equal(await page.locator('.setup-panel:visible').count(),1,'Only the chosen panel is visible');
}
async function refresh(){
  const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/runtime-config'&&r.request().method()==='GET');
  await page.locator('#setup-refresh').click();await response;
  await page.waitForFunction(()=>!document.querySelector('#setup-refresh').disabled);
}
async function save(selector,path,method='PUT'){
  const response=page.waitForResponse(r=>new URL(r.url()).pathname===path&&r.request().method()===method);
  await page.locator(selector).click();assert.equal((await response).status(),200);
  await page.waitForFunction(selector=>!document.querySelector(selector).disabled,selector);
}
async function advanced(){
  const details=panel('activity').locator('.setup-inline-details');
  if(!await details.evaluate(el=>el.open))await details.locator('summary').click();
}
async function drafts({agent='codex',projects='/demo/next-projects',watcher=false,interval='5',source='active'}={}){
  assert.equal(await page.locator('#runtime-agent').inputValue(),agent,'Agent draft survives another section update');
  assert.equal(await page.locator('#xo-root-input').inputValue(),projects,'Folder draft survives another section update');
  assert.equal(await page.locator('#runtime-watcher').isChecked(),watcher,'Activity toggle draft survives');
  assert.equal(await page.locator('#runtime-interval').inputValue(),interval,'Activity timing draft survives');
  assert.equal(await page.locator('#runtime-source-mode').inputValue(),source,'Activity source draft survives');
}
try{
  const initialRead=holdRead=gate();
  await page.goto(origin+'/space/#/secrets',{waitUntil:'domcontentloaded'});
  await initialRead.arrived.promise;
  await page.locator('#xo-root-input').fill('/demo/early-draft');
  initialRead.release.resolve();
  await page.waitForFunction(()=>!document.querySelector('#setup-refresh').disabled);
  assert.equal(await page.locator('#xo-root-input').inputValue(),'/demo/early-draft','Typing before initial status arrives is safe and preserved');
  assert.equal(await page.locator('#quirq-root-input').inputValue(),'/demo/.quirq','Initial status still fills untouched fields');
  await page.locator('#xo-root-input').fill('/demo/projects');
  await page.locator('#quirq-root-input').fill('/demo/.quirq');
  assert.doesNotMatch(await page.locator('#setup-alert').textContent(),/Unsaved changes/,'Restoring saved values clears the draft status');
  await panel('workspace').waitFor();
  assert.equal(await page.locator('.setup-panel:visible').count(),1);
  assert.deepEqual(await page.locator('#setup-nav [data-setup-go]').evaluateAll(nodes=>nodes.map(node=>node.dataset.setupGo)),
    ['workspace','agent','activity','connectors','commands','server']);
  await panel('workspace').locator('.setup-step-footer [data-setup-go="agent"]').click();
  await panel('agent').waitFor();
  assert.equal(await page.locator('#secret-form').isVisible(),false);
  await page.locator('#secret-add').click();
  await page.locator('#secret-key').fill('DEMO_DRAFT');
  assert.match(await page.locator('#setup-alert').textContent(),/Unsaved changes/,'Credential drafts are included in setup status');
  await page.locator('#secret-cancel').click();
  assert.doesNotMatch(await page.locator('#setup-alert').textContent(),/Unsaved changes/);
  assert.equal(await page.locator('#secret-add').evaluate(el=>el===document.activeElement),true);
  assert.equal(await page.locator('#setup-sources .source-row:visible').count(),1,'Other agents are collapsed');
  const agentDetails=page.locator('.source-row.is-selected .source-details');
  await agentDetails.locator('summary').click();
  assert.match(await agentDetails.textContent(),/session files? found/,'Source diagnostics remain available in details');
  await agentDetails.locator('summary').click();
  await panel('agent').locator('.setup-step-footer [data-setup-go="activity"]').click();
  await panel('activity').waitFor();
  assert.equal(await page.locator('#runtime-interval').isVisible(),false,'Detailed timing starts collapsed');
  await page.locator('#setup-open-projects').click();
  await page.waitForURL('**/#/projects');
  await page.locator('#tab-secrets').click();await panel('activity').waitFor();
  await choose('commands');assert.match(await page.locator('#command-list').textContent(),/No commands yet/);
  await choose('server');await choose('workspace');
  assert.deepEqual(writes,[],'Navigation and Next never save, run commands or restart');

  await page.locator('#xo-root-input').fill('/demo/next-projects');
  await choose('agent');await page.locator('#runtime-agent').selectOption('codex');
  assert.match(await page.locator('#setup-sources .source-row:visible').textContent(),/Codex/);
  await choose('activity');await page.locator('#runtime-watcher').uncheck();
  await page.locator('#runtime-source-mode').selectOption('active');
  await advanced();await page.locator('#runtime-interval').fill('5');
  await choose('agent');await page.locator('#secret-add').click();
  await page.locator('#secret-key').fill('DEMO_CREATED_TOKEN');
  await page.locator('#secret-value').fill('fixture-value-not-a-real-secret');
  assert.equal(await page.locator('#secret-value').getAttribute('type'),'password');
  await page.locator('#secret-toggle').click();
  assert.equal(await page.locator('#secret-value').getAttribute('type'),'text');
  await page.locator('#secret-toggle').click();
  assert.equal(await page.locator('#secret-value').getAttribute('type'),'password');
  await refresh();await drafts();
  assert.equal(await page.locator('#secret-value').inputValue(),'fixture-value-not-a-real-secret','Refresh keeps the typed credential draft');
  const pendingSecret=holdSecret=gate();
  const secretSave=save('#secret-save','/api/secrets/DEMO_CREATED_TOKEN','PATCH');
  await pendingSecret.arrived.promise;
  assert.equal(await page.locator('#secret-add').isDisabled(),true);
  assert.equal(await page.locator('#runtime-save').isDisabled(),true);
  await page.locator('#secret-add').dispatchEvent('click');
  assert.equal(await page.locator('#secret-key').inputValue(),'DEMO_CREATED_TOKEN','A pending save cannot replace its credential form');
  pendingSecret.release.resolve();await secretSave;
  await page.locator('#secret-form').waitFor({state:'hidden'});
  await page.waitForFunction(()=>!document.querySelector('#setup-refresh').disabled);
  await drafts();
  assert.equal(await page.locator('#secret-value').inputValue(),'');
  assert.doesNotMatch(await page.locator('#secret-list').textContent(),/fixture-value-not-a-real-secret/);
  assert.match(await page.locator('#secret-list').textContent(),/••••••/);
  const replace=page.locator('[data-secret-key="DEMO_CREATED_TOKEN"] [data-action="replace"]');
  await replace.click();
  assert.equal(await page.locator('#secret-key').evaluate(el=>el.readOnly),true);
  assert.equal(await page.locator('#secret-value').inputValue(),'','Replace never fetches the saved plaintext');
  await page.locator('#secret-cancel').click();
  await page.locator('#setup-sources .source-row:visible [data-secret-key="DEMO_API_KEY"]').click();
  assert.equal(await page.locator('#secret-key').inputValue(),'DEMO_API_KEY');
  await page.locator('#secret-cancel').click();

  const staleRead=holdRead=gate();
  await page.locator('#setup-refresh').click();await staleRead.arrived.promise;
  await save('#runtime-save','/api/runtime-config');
  staleRead.release.resolve();
  await page.waitForFunction(()=>!document.querySelector('#setup-refresh').disabled);
  const agentSave=writes.filter(write=>write.path==='/api/runtime-config').at(-1).body;
  assert.deepEqual(agentSave,{agent_name:'codex',watcher_enabled:true,watcher_interval_seconds:1,watcher_source_mode:'all'},
    'Agent Save retains the last saved activity settings, not its unsaved draft');
  await drafts();
  await page.locator('#runtime-agent').selectOption('claude_code');
  await choose('activity');await save('#activity-save','/api/runtime-config');
  const activitySave=writes.filter(write=>write.path==='/api/runtime-config').at(-1).body;
  assert.deepEqual(activitySave,{agent_name:'codex',watcher_enabled:false,watcher_interval_seconds:5,watcher_source_mode:'active'},
    'Activity Save retains the saved agent, not the other panel draft');
  await drafts({agent:'claude_code'});
  await choose('workspace');await save('#roots-save','/api/runtime-config/roots');
  assert.deepEqual(writes.filter(write=>write.path.endsWith('/roots')).at(-1).body,
    {xo_projects_root:'/demo/next-projects',quirq_state_root:'/demo/.quirq'});
  await drafts({agent:'claude_code'});
  assert.match(await page.locator('#setup-alert').textContent(),/Unsaved changes/);
  await choose('agent');await save('#runtime-save','/api/runtime-config');
  assert.match(await page.locator('#setup-alert').textContent(),/folder/i);
  assert.equal(await page.locator('#setup-alert [data-setup-go="workspace"]').count(),1);
  await choose('server');
  assert.match(await page.locator('#setup-restart-hint').textContent(),/Pending changes: folders, agent or activity settings, credentials/);
  assert.equal(await page.locator('#runtime-restart').isVisible(),true);
  assert.equal(await page.locator('#setup-restart').isVisible(),false,'Show one relevant restart action');
  await page.locator('#runtime-restart').click();
  await page.waitForFunction(()=>document.querySelector('#setup-restart-error').textContent.includes('already in progress'));
  assert.equal(await page.locator('#runtime-restart').isEnabled(),true,'Failed restart leaves the action usable');
  restartMode='foreground';await refresh();
  assert.equal(await page.locator('#runtime-restart').isDisabled(),true);
  assert.match(await page.locator('#setup-restart-hint').textContent(),/Ctrl-C/i);

  failRuntime=true;await refresh();
  assert.match(await page.locator('#setup-alert').textContent(),/Settings unavailable/);
  assert.equal(await page.locator('#roots-save').isDisabled(),true);
  assert.equal(await page.locator('#runtime-save').isDisabled(),true);
  assert.equal(await page.locator('#activity-save').isDisabled(),true);
  failRuntime=false;await refresh();
  assert.equal(await page.locator('#runtime-save').isEnabled(),true);

  /* Capture ordinary applied settings, with no unsaved input or error overlays. */
  restartMode='native';secretRevision=0;
  fixture.applied=structuredClone(fixture.configured);
  fixture.roots.applied=structuredClone(fixture.roots.configured);
  await page.reload({waitUntil:'networkidle'});await panel('workspace').waitFor();
  for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:1000});
    for(const id of ['workspace','agent','activity','commands','server']){
      await choose(id);
      await page.waitForTimeout(350);
      assert.ok(await page.locator('#setup-nav,.setup-panel:visible,.setup-panel:visible input,.setup-panel:visible select,.setup-panel:visible button').evaluateAll(nodes=>nodes.filter(el=>el.getClientRects().length).every(el=>{
        const rect=el.getBoundingClientRect();return rect.left>=-1&&rect.right<=innerWidth+1;
      })),`${width}px ${id} navigation and controls fit`);
      await page.screenshot({path:resolve(output,`setup-${id}-${width}.png`)});
    }
  }
  assert.deepEqual(errors,[]);
  assert.equal(writes.some(write=>write.path.startsWith('/api/schedules')),false);
  assert.equal(writes.filter(write=>write.path==='/space/server/restart').length,1);
  console.log('Setup journey: panel/Next navigation, no automatic writes, scoped saves, cross-form drafts, masked credentials, pending/unsupported restart and1440/390/320px layouts passed.');
}catch(error){
  await page.screenshot({path:resolve(output,'failure.png')});throw error;
}finally{
  await writeFile(resolve(output,'report.json'),JSON.stringify({errors,writes},null,2)+'\n');
  await browser.close();
}
