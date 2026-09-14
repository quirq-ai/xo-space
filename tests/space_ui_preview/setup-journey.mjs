/* Guided Setup regression. All setting/credential/restart writes are handled
   in browser memory; this never changes an installation or executes commands. */
import assert from 'node:assert/strict';
import {openProjectList} from './routes.mjs';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5100';
const endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');
assert.notEqual(endpoint.port,'5002');
const output=resolve(process.argv[2]||'/tmp/space-setup-journey-review');
const sections=['workspace','intelligence','projects','connectors','secrets','commands','server'];
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
  if(path==='/xo-auth/session/self'){assert.equal(method,'GET');return send(route,{session_id:'fictional-route-test-session'});}
  if(path==='/api/connectors/composio/toolkits'){assert.equal(method,'GET');return send(route,{toolkits:[]});}
  if(path==='/api/connections'){assert.equal(method,'GET');return send(route,{signed_in:true,poller_enabled:false,connections:[]});}
  if(/^\/api\/connectors\/(github|vercel)\/status$/.test(path)){assert.equal(method,'GET');return send(route,{status:'needs_auth'});}
  if(path==='/api/connectors/magicpath/status'){assert.equal(method,'GET');return send(route,{cli_installed:false,skill_installed:false,logged_in:false,user:null});}
  if(/^\/api\/connectors\/(gdrive|onedrive)\/remotes$/.test(path)){assert.equal(method,'GET');return send(route,{remotes:[]});}
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
async function expectSection(id){
  await page.waitForURL('**/#/setup/'+id);await panel(id).waitFor();
  assert.equal(await page.locator('.setup-panel:visible').count(),1,'Only the routed section is visible');
  assert.equal(await page.locator('#section-nav [data-section-page][aria-current="page"]').count(),1);
  assert.equal(await page.locator('#section-nav [data-setup-go="'+id+'"]').getAttribute('aria-current'),'page');
  assert.equal(await page.locator('#tab-setup.is-on').count(),1,'Section URLs share the Setup primary tab');
}
async function choose(id){
  await page.locator(`#section-nav [data-setup-go="${id}"]`).click();
  await expectSection(id);
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
  const details=panel('intelligence').locator('.setup-inline-details');
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
  await page.goto(origin+'/space/#/setup',{waitUntil:'domcontentloaded'});
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
  assert.deepEqual(await page.locator('#section-nav [data-setup-go]').evaluateAll(nodes=>nodes.map(node=>node.dataset.setupGo)),
    sections);
  assert.deepEqual(await page.locator('#section-nav [data-setup-go]').evaluateAll(nodes=>nodes.map(node=>({tag:node.tagName,href:node.getAttribute('href')}))),
    sections.map(id=>({tag:'A',href:'#/setup/'+id})),'Every sidebar section has a copyable, openable canonical link');
  await panel('workspace').locator('.setup-step-footer [data-setup-go="intelligence"]').click();
  await panel('intelligence').waitFor();
  assert.equal(await panel('intelligence').locator('#runtime-form,#activity-form').count(),2,'Intelligence contains both configuration forms');
  assert.equal(await panel('intelligence').locator('#setup-projects').count(),0,'Project management is a separate section');
  assert.equal(await page.locator('#setup-panel-agent,#setup-panel-activity').count(),0,'Old section containers are gone');
  assert.equal(await page.locator('#secret-form').isVisible(),false);
  assert.equal(await panel('intelligence').locator('#secret-form,#secret-list').count(),0,'Credential management has its own section');
  await panel('intelligence').getByRole('button',{name:/manage secrets/i}).click();
  await panel('secrets').waitFor();await page.waitForURL('**/#/setup/secrets');
  await page.locator('#secret-add').click();
  await page.locator('#secret-key').fill('DEMO_DRAFT');
  assert.match(await page.locator('#setup-alert').textContent(),/Unsaved changes/,'Credential drafts are included in setup status');
  assert.match(await page.locator('#section-nav [data-setup-go="secrets"]').textContent(),/Unsaved changes/,'Credential draft status belongs to Secrets');
  assert.doesNotMatch(await page.locator('#section-nav [data-setup-go="intelligence"]').textContent(),/Unsaved changes/,'A credential draft does not mark Intelligence as unsaved');
  await page.locator('#secret-cancel').click();
  assert.doesNotMatch(await page.locator('#setup-alert').textContent(),/Unsaved changes/);
  assert.equal(await page.locator('#secret-add').evaluate(el=>el===document.activeElement),true);
  await choose('intelligence');
  assert.equal(await page.locator('#setup-sources .source-row:visible').count(),1,'Other agents are collapsed');
  const agentDetails=page.locator('.source-row.is-selected .source-details');
  await agentDetails.locator('summary').click();
  assert.match(await agentDetails.textContent(),/session files? found/,'Source diagnostics remain available in details');
  await agentDetails.locator('summary').click();
  assert.equal(await page.locator('#runtime-interval').isVisible(),false,'Detailed timing starts collapsed');
  await panel('intelligence').locator('.setup-step-footer [data-setup-go="projects"]').click();
  await panel('projects').waitFor();
  assert.equal(await panel('projects').locator('#setup-projects').count(),1);
  assert.equal(await panel('projects').locator('#runtime-form,#activity-form').count(),0);
  await page.locator('#setup-open-projects').click();
  await page.waitForURL('**/#/projects/list');
  await page.locator('#tab-setup').click();await panel('workspace').waitFor();
  assert.equal(new URL(page.url()).hash,'#/setup/workspace','The primary Setup button consistently opens Workspace');
  for(const legacy of ['agent','activity']){
    await choose('workspace');
    await page.evaluate(panel=>dispatchEvent(new CustomEvent('space:setup-section',{detail:{panel}})),legacy);
    await panel('intelligence').waitFor();
    assert.equal(await page.locator('#section-nav [data-setup-go="intelligence"]').getAttribute('aria-current'),'page');
    assert.equal(new URL(page.url()).hash,'#/setup/intelligence','Legacy section handoffs use the canonical section URL');
    const control=legacy==='agent'?'#runtime-agent':'#runtime-source-mode';
    assert.equal(await page.locator(control).evaluate(node=>node===document.activeElement),true,'Legacy '+legacy+' handoff focuses its corresponding Intelligence control');
  }
  await choose('projects');await page.locator('#view-search').fill('activity interval');
  await page.locator('.setup-search-result').filter({hasText:'Activity interval'}).click();
  await panel('intelligence').waitFor();
  assert.equal(await page.locator('#runtime-interval').isVisible(),true,'Search reveals the advanced activity control in Intelligence');
  assert.equal(await page.locator('#runtime-interval').evaluate(node=>node===document.activeElement),true);
  assert.equal(await page.locator('#view-search').inputValue(),'');
  await choose('commands');assert.match(await page.locator('#command-list').textContent(),/No commands yet/);
  await choose('server');await choose('workspace');
  assert.deepEqual(writes,[],'Navigation and Next never save, run commands or restart');

  await page.locator('#xo-root-input').fill('/demo/next-projects');
  await choose('intelligence');await page.locator('#runtime-agent').selectOption('codex');
  assert.match(await page.locator('#section-nav [data-setup-go="intelligence"]').textContent(),/Unsaved changes/);
  assert.doesNotMatch(await page.locator('#section-nav [data-setup-go="projects"]').textContent(),/Unsaved changes/,'Runtime drafts do not mark Projects as unsaved');
  assert.match(await page.locator('#setup-sources .source-row:visible').textContent(),/Codex/);
  await choose('intelligence');await page.locator('#runtime-watcher').uncheck();
  await page.locator('#runtime-source-mode').selectOption('active');
  await advanced();await page.locator('#runtime-interval').fill('5');
  await choose('secrets');await page.locator('#secret-add').click();
  await page.locator('#secret-key').fill('DEMO_CREATED_TOKEN');
  await page.locator('#secret-value').fill('fixture-value-not-a-real-secret');
  assert.equal(await page.locator('#secret-value').getAttribute('type'),'password');
  await page.locator('#secret-toggle').click();
  assert.equal(await page.locator('#secret-value').getAttribute('type'),'text');
  await page.locator('#secret-toggle').click();
  assert.equal(await page.locator('#secret-value').getAttribute('type'),'password');
  const credentialNode=await page.locator('#secret-value').elementHandle();
  for(const id of ['intelligence','projects','workspace','secrets'])await choose(id);
  assert.equal(await credentialNode.evaluate(node=>node.isConnected),true,'Section navigation retains the credential form');
  assert.equal(await page.locator('#secret-value').inputValue(),'fixture-value-not-a-real-secret','Section navigation retains the credential draft');
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
  await choose('intelligence');
  await page.locator('#setup-sources .source-row:visible [data-secret-key="DEMO_API_KEY"]').click();
  await panel('secrets').waitFor();await page.waitForURL('**/#/setup/secrets');
  assert.equal(await page.locator('#secret-key').inputValue(),'DEMO_API_KEY');
  assert.equal(await page.locator('#secret-key').evaluate(el=>el.readOnly),true,'Recommended Agent keys open the Secrets form with the key selected');
  assert.equal(await page.locator('#secret-value').evaluate(el=>el===document.activeElement),true,'Recommended keys focus the new value');
  await page.locator('#secret-cancel').click();
  await choose('intelligence');

  const staleRead=holdRead=gate();
  await page.locator('#setup-refresh').click();await staleRead.arrived.promise;
  await save('#runtime-save','/api/runtime-config');
  staleRead.release.resolve();
  await page.waitForFunction(()=>!document.querySelector('#setup-refresh').disabled);
  const agentSave=writes.filter(write=>write.path==='/api/runtime-config').at(-1).body;
  assert.deepEqual(agentSave,{agent_name:'codex',watcher_enabled:true,watcher_interval_seconds:1,watcher_source_mode:'all'},
    'Agent Save retains the last saved activity settings, not its unsaved draft');
  await drafts();
  assert.match(await page.locator('#section-nav [data-setup-go="intelligence"]').textContent(),/Unsaved changes/,'Saving the agent keeps the unsaved activity form visible in Intelligence status');
  await page.locator('#runtime-agent').selectOption('claude_code');
  await choose('intelligence');await save('#activity-save','/api/runtime-config');
  const activitySave=writes.filter(write=>write.path==='/api/runtime-config').at(-1).body;
  assert.deepEqual(activitySave,{agent_name:'codex',watcher_enabled:false,watcher_interval_seconds:5,watcher_source_mode:'active'},
    'Activity Save retains the saved agent, not its neighboring form draft');
  await drafts({agent:'claude_code'});
  assert.match(await page.locator('#section-nav [data-setup-go="intelligence"]').textContent(),/Unsaved changes/,'Saving activity keeps an unsaved agent choice');
  assert.doesNotMatch(await page.locator('#section-nav [data-setup-go="projects"]').textContent(),/Unsaved changes/);
  await choose('workspace');await save('#roots-save','/api/runtime-config/roots');
  assert.deepEqual(writes.filter(write=>write.path.endsWith('/roots')).at(-1).body,
    {xo_projects_root:'/demo/next-projects',quirq_state_root:'/demo/.quirq'});
  await drafts({agent:'claude_code'});
  assert.match(await page.locator('#setup-alert').textContent(),/Unsaved changes/);
  await choose('intelligence');await save('#runtime-save','/api/runtime-config');
  assert.doesNotMatch(await page.locator('#section-nav [data-setup-go="intelligence"]').textContent(),/Unsaved changes/,'Intelligence clears only after both form drafts are saved');
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
  await choose('projects');await page.locator('#setup-project-list .setup-project-row').first().waitFor();
  assert.equal(await page.locator('#setup-project-add').isEnabled(),true,'Runtime status failure does not disable project management');
  await page.locator('#setup-project-add').click();
  await page.locator('#setup-project-repository').fill('https://github.com/fixture/available-without-runtime.git');
  assert.equal(await page.locator('#setup-project-create').isEnabled(),true);
  assert.equal(await page.locator('#setup-project-id').inputValue(),'available-without-runtime');
  await page.locator('#setup-project-cancel').click();
  failRuntime=false;await refresh();
  assert.equal(await page.locator('#runtime-save').isEnabled(),true);

  /* Capture ordinary applied settings, with no unsaved input or error overlays. */
  restartMode='native';secretRevision=0;
  fixture.applied=structuredClone(fixture.configured);
  fixture.roots.applied=structuredClone(fixture.roots.configured);
  await page.goto(origin+'/space/#/secrets',{waitUntil:'networkidle'});await panel('secrets').waitFor();
  assert.equal(await page.locator('#tab-setup.is-on').count(),1,'The Secrets deep link highlights Setup');
  assert.equal(await page.locator('#tab-secrets,#view-secrets').count(),0,'Secrets uses the persistent Setup shell');
  assert.equal(await page.locator('#secret-form').isVisible(),false,'A Secrets deep link opens the list without a credential draft');
  await choose('workspace');
  await page.reload({waitUntil:'networkidle'});await panel('workspace').waitFor();
  for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:1000});
    for(const id of ['workspace','intelligence','projects','secrets','commands','server']){
      await choose(id);
      await page.waitForTimeout(350);
      assert.ok(await page.locator('#section-nav,.setup-panel:visible,.setup-panel:visible input,.setup-panel:visible select,.setup-panel:visible button').evaluateAll(nodes=>nodes.filter(el=>el.getClientRects().length).every(el=>{
        const rect=el.getBoundingClientRect();return rect.left>=-1&&rect.right<=innerWidth+1;
      })),`${width}px ${id} navigation and controls fit`);
      await page.screenshot({path:resolve(output,`setup-${id}-${width}.png`)});
    }
  }
  await page.setViewportSize({width:1440,height:1000});
  const writesBeforeRouting=writes.length;
  for(const id of sections){
    await page.goto(origin+'/space/#/setup/'+id,{waitUntil:'networkidle'});await expectSection(id);
    await page.reload({waitUntil:'networkidle'});await expectSection(id);
  }
  for(const [alias,id] of [['setup','workspace'],['connectors','connectors'],['secrets','secrets']]){
    await page.goto(origin+'/space/#/projects/list',{waitUntil:'networkidle'});
    await page.goto(origin+'/space/#/'+alias,{waitUntil:'networkidle'});await expectSection(id);
    await page.goBack();await page.waitForURL('**/#/projects/list');
    assert.equal(await page.locator('#view-projects.is-active').count(),1,'Alias normalization does not add an extra history entry');
    await page.goForward();await expectSection(id);
  }
  await page.goto(origin+'/space/#/setup/workspace',{waitUntil:'networkidle'});await expectSection('workspace');
  await page.locator('#xo-root-input').fill('/demo/history-folder-draft');
  const folderNode=await page.locator('#xo-root-input').elementHandle();
  await choose('intelligence');await page.locator('#runtime-agent').selectOption('codex');
  const agentNode=await page.locator('#runtime-agent').elementHandle();
  await choose('projects');await page.locator('#setup-project-add').click();
  await page.locator('#setup-project-repository').fill('https://github.com/fixture/history-draft.git');
  const projectNode=await page.locator('#setup-project-repository').elementHandle();
  await choose('connectors');await choose('secrets');await page.locator('#secret-add').click();
  await page.locator('#secret-key').fill('DEMO_HISTORY_DRAFT');await page.locator('#secret-value').fill('fictional-history-secret');
  const secretNode=await page.locator('#secret-value').elementHandle();
  await choose('commands');await choose('server');
  const historyLength=await page.evaluate(()=>history.length);
  await choose('server');assert.equal(await page.evaluate(()=>history.length),historyLength,'Clicking the current section does not duplicate browser history');
  for(const id of sections.slice(0,-1).reverse()){await page.goBack();await expectSection(id);}
  for(const id of sections.slice(1)){await page.goForward();await expectSection(id);}
  for(const handle of [folderNode,agentNode,projectNode,secretNode])assert.equal(await handle.evaluate(node=>node.isConnected),true,'Browser history retains the same mounted draft controls');
  assert.equal(await page.locator('#xo-root-input').inputValue(),'/demo/history-folder-draft');
  assert.equal(await page.locator('#runtime-agent').inputValue(),'codex');
  assert.equal(await page.locator('#setup-project-repository').inputValue(),'https://github.com/fixture/history-draft.git');
  assert.equal(await page.locator('#secret-value').inputValue(),'fictional-history-secret');
  await page.locator('#tab-setup').click();await expectSection('workspace');
  await choose('secrets');assert.equal(await page.locator('#secret-value').inputValue(),'fictional-history-secret','Primary Setup navigation keeps drafts while returning to Workspace');
  assert.equal(writes.length,writesBeforeRouting,'Deep links, reloads, alias normalization and browser history never save drafts or mutate a project');
  assert.deepEqual(errors,[]);
  assert.equal(writes.some(write=>write.path.startsWith('/api/schedules')),false);
  assert.equal(writes.filter(write=>write.path==='/space/server/restart').length,1);
  console.log('Setup journey: every section deep link/reload, alias normalization, back/forward, retained drafts, Workspace → Intelligence → Projects, legacy focus, search, scoped saves, restart states and 1440/390/320px layouts passed.');
}catch(error){
  await page.screenshot({path:resolve(output,'failure.png')});throw error;
}finally{
  await writeFile(resolve(output,'report.json'),JSON.stringify({errors,writes},null,2)+'\n');
  await browser.close();
}
