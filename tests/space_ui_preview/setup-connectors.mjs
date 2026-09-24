/* Connectors inside guided Setup. Every connector/session response and write
   is fictional browser memory; no provider, local settings or jobs are changed. */
import assert from 'node:assert/strict';
import {installRefreshProbes,waitForSetup} from './refresh-helpers.mjs';
import {openProjectList} from './routes.mjs';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5100';
const endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');
assert.notEqual(endpoint.port,'5002');
assert.notEqual(endpoint.port,'5112','Use the read-only fixture, preserving the interactive Commands server');
const output=resolve(process.argv[2]||'/tmp/space-setup-connectors');
await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE
  ?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const browser=await chromium.launch({headless:true,
  executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
const report={origin,checks:[],requests:[],writes:[],errors:[],screenshots:[]};
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
const gate=()=>({arrived:deferred(),release:deferred()});
let holdList=null,holdPrefs=null,holdStatus=null,holdRuntime=null;
let authorized=false;
const settings=new Map();
const connection=id=>({toolkit:id,configured:id!=='telegram',enabled:id!=='telegram',interval_s:900,
  collectors:['recent'],available_collectors:[
    {id:'recent',label:'Recent messages',default:true},{id:'calendar',label:'Calendar events',default:false}],
  account_label:id==='gmail'?'demo@example.com':id==='slack'?'team@example.com':'Demo bot',
  account_checked_at:'2026-09-14T10:00:00Z',events_total:2,last_poll_at:'2026-09-14T10:00:00Z',
  ...settings.get(id)});
const toolkits=()=>[
  {id:'gmail',display_name:'Gmail',description:'Read messages and inbox'},
  {id:'slack',display_name:'Slack',description:'Team conversation'},
  {id:'telegram',display_name:'Telegram',description:'Bot messages'},
].map((toolkit,index)=>({...toolkit,slug:toolkit.id,
  status:index<2||authorized?'ACTIVE':'',workspace_enabled:index<2,
  supports_action_prefs:true,schemes:[index===2?'API_KEY':'OAUTH2'],connected_account_id:'fixture-'+index}));
const json=(route,data)=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
async function pause(pending){
  if(pending){pending.arrived.resolve();await pending.release.promise;}
}
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),path=url.pathname,method=request.method();
  if(url.origin!==endpoint.origin){
    report.errors.push('Blocked external request: '+request.url());return route.abort();
  }
  const related=path==='/xo-auth/session/self'||path.startsWith('/api/connectors/')||path.startsWith('/api/connections');
  if(related)report.requests.push({path,method});
  if(method!=='GET')report.writes.push({path,method,body:request.postDataJSON()});
  if(/^\/api\/connectors\/(github|vercel)\/status$/.test(path)&&method==='GET')return json(route,{status:'needs_auth'});
  if(path==='/api/connectors/magicpath/status'&&method==='GET')return json(route,{cli_installed:false,skill_installed:false,logged_in:false,user:null});
  if(/^\/api\/connectors\/(gdrive|onedrive)\/remotes$/.test(path)&&method==='GET')return json(route,{remotes:[]});
  if(path==='/api/runtime-config'&&method==='GET'){
    const pending=holdRuntime;holdRuntime=null;await pause(pending);return route.continue();
  }
  if(path==='/xo-auth/session/self')return json(route,{session_id:'fictional-setup-session'});
  if(path==='/api/connectors/composio/toolkits'){
    const pending=holdList;holdList=null;await pause(pending);return json(route,{toolkits:toolkits()});
  }
  if(path==='/api/connections')return json(route,{signed_in:true,poller_enabled:true,
    connections:toolkits().map(toolkit=>connection(toolkit.id))});
  const local=path.match(/^\/api\/connections\/(gmail|slack|telegram)(?:\/(account|poll))?$/);
  if(local){
    const [,id,action]=local;
    if(action==='account')return json(route,connection(id));
    if(action==='poll'){
      assert.equal(method,'POST');return json(route,{new_events:1});
    }
    if(method==='PUT')settings.set(id,{...request.postDataJSON(),configured:true});
    else assert.equal(method,'GET');
    return json(route,connection(id));
  }
  const provider=path.match(/^\/api\/connectors\/composio\/(gmail|slack|telegram)\/(tools|prefs|connect|status)$/);
  if(provider){
    const [,id,action]=provider;
    if(action==='tools')return json(route,{tools:[{slug:'send',name:'Send message',enabled:true}]});
    if(action==='prefs'){
      assert.equal(method,'PUT');const pending=holdPrefs;holdPrefs=null;await pause(pending);
      return json(route,{ok:true});
    }
    if(action==='connect'){
      assert.equal(method,'POST');assert.equal(id,'telegram');
      assert.deepEqual(request.postDataJSON(),{auth_scheme:'API_KEY',allow_multiple:false});
      return json(route,{auth_url:origin+'/fixture-authorization',connection_request_id:'fixture-request'});
    }
    const pending=holdStatus;holdStatus=null;await pause(pending);authorized=true;
    return json(route,{status:'ACTIVE'});
  }
  if(path==='/fixture-authorization')return route.fulfill({contentType:'text/html',body:'<p>Fictional authorization</p>'});
  if(related||method!=='GET'){
    report.errors.push('Blocked unexpected request: '+method+' '+path);
    return json(route,{error:'Unexpected request blocked by Setup fixture'});
  }
  return route.continue();
});
await installRefreshProbes(context);
function observe(page){
  page.on('pageerror',error=>report.errors.push(error.message));
  page.on('console',message=>{if(message.type()==='error')report.errors.push(message.text());});
  page.on('response',response=>{if(response.status()>=400)report.errors.push(response.status()+' '+response.url());});
}
const page=await context.newPage();observe(page);
const panel=id=>page.locator('#setup-panel-'+id);
async function choose(id){
  await page.locator('#setup-nav [data-setup-go="'+id+'"]').click();
  await panel(id).waitFor();
  await page.waitForFunction(({id,mode})=>location.hash==='#/setup/'+id
    &&document.querySelector('.topbar')?.dataset.toolbar===mode,{id,mode:'search'});
  assert.equal(await page.locator('.setup-panel:visible').count(),1);
  assert.equal(await page.locator('#tab-setup.is-on').count(),1);
}
const count=path=>report.requests.filter(request=>request.path===path).length;
function checked(text){report.checks.push(text);console.log(text);}
async function shot(name){
  await page.screenshot({path:resolve(output,name)});report.screenshots.push(name);
}

try{
  await page.goto(origin+'/space/#/setup',{waitUntil:'networkidle'});
  await panel('workspace').waitFor();
  await waitForSetup(page);
  assert.deepEqual(await page.locator('.tabs a').evaluateAll(nodes=>nodes.map(node=>node.id)),
    ['tab-projects','tab-agents','tab-inbox','tab-setup']);
  assert.deepEqual(await page.locator('#setup-nav [data-setup-go]').evaluateAll(nodes=>nodes.map(node=>node.dataset.setupGo)),
    ['workspace','intelligence','projects','connectors','secrets','commands','server']);
  for(const id of ['intelligence','projects','secrets','commands','server','workspace'])await choose(id);
  assert.deepEqual(report.requests,[],'Ordinary Setup never mints a session, lists toolkits or resolves connector accounts');
  assert.deepEqual(report.writes,[]);
  checked('Four primary tabs; Connectors is under Manage; ordinary Setup navigation makes no connector/session requests or writes.');

  const setupSearch=page.locator('#view-search');
  for(const id of ['workspace','intelligence','projects','secrets','commands','server']){
    await choose(id);
    assert.equal(await setupSearch.isVisible(),true,id+' keeps Setup search visible');
    assert.equal(await setupSearch.getAttribute('placeholder'),'Search setup…');
  }
  await setupSearch.fill('folder');
  await page.locator('.setup-search-result').filter({hasText:'Projects folder'}).click();
  assert.equal(await page.locator('#xo-root-input').evaluate(node=>node===document.activeElement),true);
  assert.equal(await setupSearch.inputValue(),'');
  await page.locator('#xo-root-input').fill('/demo/unsaved-connectors-test');
  const folderNode=await page.locator('#xo-root-input').elementHandle();
  await setupSearch.fill('secret');
  await page.locator('.setup-search-result').filter({hasText:/^Secrets/}).click();
  await panel('secrets').waitFor();
  assert.equal(new URL(page.url()).hash,'#/setup/secrets');
  assert.equal(await folderNode.evaluate(node=>node.isConnected),true);
  await page.locator('#secret-add').click();
  await page.locator('#secret-key').fill('DEMO_UNSAVED_TOKEN');
  await page.locator('#secret-value').fill('fictional-unsaved-value');
  const credentialNode=await page.locator('#secret-value').elementHandle();
  await setupSearch.fill('fictional-unsaved-value');
  assert.equal(await page.locator('.setup-search-result').count(),0,'Secret values never enter the search index');
  await setupSearch.fill('DEMO_UNSAVED_TOKEN');
  assert.equal(await page.locator('.setup-search-result').count(),0,'Secret key drafts never enter the search index');
  await setupSearch.fill('folder');
  await openProjectList(page);await page.waitForURL('**/#/projects/data/list');
  await page.locator('#tab-setup').click();
  await panel('workspace').waitFor();
  assert.equal(await setupSearch.inputValue(),'','Returning through Setup opens Workspace with its search cleared');
  await choose('secrets');
  assert.equal(await setupSearch.inputValue(),'','Sidebar selection clears Setup query');
  assert.equal(await page.locator('#secret-value').inputValue(),'fictional-unsaved-value');
  assert.equal(await credentialNode.evaluate(node=>node.isConnected),true);
  checked('All Setup sections keep search; results navigate and focus without losing folder/secret drafts or indexing private form values.');
  const listing=holdList=gate();
  await choose('connectors');await listing.arrived.promise;
  const host=await page.locator('#setup-connectors').elementHandle();
  await choose('workspace');
  listing.release.resolve();
  await page.locator('#setup-connectors .conn-tile').first().waitFor({state:'attached'});
  assert.equal(new URL(page.url()).hash,'#/setup/workspace');
  assert.equal(await panel('workspace').isVisible(),true,'A delayed connector mount cannot reclaim the current panel');
  assert.equal(await page.locator('.topbar').getAttribute('data-toolbar'),'search');
  assert.equal(await folderNode.evaluate(node=>node===document.activeElement),false);
  assert.equal(await page.locator('#setup-workspace-title').evaluate(node=>node===document.activeElement),true,
    'A delayed mount cannot steal panel heading focus');
  await choose('connectors');
  assert.equal(count('/xo-auth/session/self'),1);assert.equal(count('/api/connectors/composio/toolkits'),1);
  assert.equal(await page.locator('#view-connectors').count(),0);
  assert.equal(await page.locator('#setup-connectors .conn-tile[data-toolkit]').count(),3);
  checked('First connector load is lazy and shared; slow completion preserves the later panel, toolbar and focus.');

  /* a tile opens its connector in a popup, and every control lives there */
  await page.locator('.conn-tile[data-toolkit="gmail"]').click();
  await page.locator('#conn-modal .conn-card[data-toolkit="gmail"]').waitFor();
  await page.locator('#conn-modal [data-action="polling"]').click();
  const interval=page.locator('#poll-gmail [data-poll="interval"]');
  await interval.selectOption('1800');
  await page.locator('#poll-gmail [data-poll="collector"][value="calendar"]').check();
  await page.locator('#conn-modal [data-action="actions"]').click();
  const action=page.locator('#conn-modal input[data-action="toggle"]');
  await action.waitFor();
  const pollNode=await interval.elementHandle(),actionNode=await action.elementHandle();
  const pendingPrefs=holdPrefs=gate();await action.uncheck();await pendingPrefs.arrived.promise;
  const search=page.locator('#view-search');await search.fill('gmail');
  await choose('secrets');
  assert.equal(await page.locator('#secret-value').inputValue(),'fictional-unsaved-value');
  await choose('workspace');
  assert.equal(await page.locator('#xo-root-input').inputValue(),'/demo/unsaved-connectors-test');
  await choose('connectors');assert.equal(await search.inputValue(),'gmail');
  await openProjectList(page);await page.waitForURL('**/#/projects/data/list');
  await page.locator('#tab-setup').click();await panel('workspace').waitFor();await choose('connectors');
  assert.equal(await search.inputValue(),'gmail');
  for(const handle of [host,folderNode,credentialNode,pollNode,actionNode])
    assert.equal(await handle.evaluate(node=>node.isConnected),true,'Setup and connector controls retain their DOM nodes');
  assert.equal(await interval.inputValue(),'1800');
  assert.equal(await action.isDisabled(),true,'Pending action stays busy across panel and top-level navigation');
  assert.equal(count('/api/connectors/composio/toolkits'),1,'Panel and tab navigation never remounts the connector controller');
  pendingPrefs.release.resolve();await page.waitForFunction(()=>!document.querySelector('#conn-modal input[data-action="toggle"]').disabled);
  assert.equal(await action.isChecked(),false);
  await search.fill('');
  await page.locator('#conn-modal [data-action="poll-save"]').click();
  await page.waitForFunction(()=>!document.querySelector('#conn-modal [data-action="poll-save"]').disabled);
  assert.deepEqual(settings.get('gmail'),{enabled:true,interval_s:1800,collectors:['recent','calendar'],configured:true});
  await page.locator('#conn-modal [data-action="poll-now"]').click();
  await page.getByText('Polled just now: 1 new',{exact:false}).waitFor();
  checked('Search, polling drafts, settings/credential drafts and pending action controls survive panel and top-level navigation; polling saves and runs through fixture APIs.');

  const authorization=holdStatus=gate();
  await page.locator('.conn-tile[data-toolkit="telegram"]').click();
  await page.locator('#conn-modal [data-action="connect"]').click();
  await authorization.arrived.promise;
  await choose('server');await openProjectList(page);await page.waitForURL('**/#/projects/data/list');
  authorization.release.resolve();
  await page.locator('#poll-telegram [data-poll="interval"]').waitFor({state:'attached'});
  assert.equal(new URL(page.url()).hash,'#/projects/data/list','Completing authorization cannot navigate away from the current tab');
  await page.locator('#tab-setup').click();await panel('workspace').waitFor();
  await choose('connectors');await page.locator('#poll-telegram').waitFor();
  assert.match(await page.locator('.conn-tile[data-toolkit="telegram"]').textContent(),/Off in this workspace/);
  checked('Authorization completes while Setup is hidden and retains its polling follow-up without stealing navigation.');

  for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:1000});
    await choose('connectors');
    const bounds=await page.locator('#setup-nav,#setup-panel-connectors,.conn-tile,.conn-modal-panel,.conn-modal-panel button,.conn-poll select').evaluateAll(nodes=>
      nodes.filter(node=>node.getClientRects().length).map(node=>{
        const rect=node.getBoundingClientRect();return{tag:node.tagName,cls:node.className,left:rect.left,right:rect.right};
      }));
    assert.ok(bounds.every(rect=>rect.left>=-1&&rect.right<=width+1),width+'px connector controls fit: '+JSON.stringify(bounds));
    await shot('setup-connectors-'+width+'.png');
  }
  checked('Connectors navigation, tiles and the popup\u2019s polling controls fit at 1440px, 390px and 320px.');

  const direct=await context.newPage();observe(direct);
  const before=count('/api/connectors/composio/toolkits');
  const runtime=holdRuntime=gate();
  await direct.goto(origin+'/space/#/connectors',{waitUntil:'domcontentloaded'});
  await runtime.arrived.promise;
  await direct.locator('#setup-panel-connectors .conn-tile').first().waitFor({timeout:5000});
  assert.equal(await direct.locator('#tab-setup.is-on').count(),1);
  assert.equal(await direct.locator('#view-setup.is-active').count(),1);
  assert.equal(await direct.locator('#tab-connectors,#view-connectors').count(),0);
  assert.equal(count('/api/connectors/composio/toolkits'),before+1);
  await direct.locator('#setup-nav [data-setup-go="workspace"]').click();
  await direct.waitForURL('**/#/setup/workspace');
  await openProjectList(direct);await direct.waitForURL('**/#/projects/data/list');
  const runtimeResponse=direct.waitForResponse(response=>new URL(response.url()).pathname==='/api/runtime-config');
  runtime.release.resolve();await runtimeResponse;
  await waitForSetup(direct);
  assert.equal(new URL(direct.url()).hash,'#/projects/data/list','A delayed initial Setup read cannot reclaim navigation');
  assert.equal(await direct.locator('#view-projects.is-active').count(),1);
  await direct.locator('#tab-setup').click();
  await direct.locator('#setup-panel-workspace').waitFor();
  await direct.locator('#setup-nav [data-setup-go="connectors"]').click();
  await direct.waitForURL('**/#/setup/connectors');
  assert.equal(count('/api/connectors/composio/toolkits'),before+1,'Direct alias and Setup share one mounted controller');
  await direct.close();
  checked('The legacy deep link loads Connectors while initial Setup status is pending; completing that read preserves subsequent navigation and one shared mount.');

  await page.setViewportSize({width:1440,height:1000});
  const beforeLinks=count('/api/connectors/composio/toolkits');
  await page.locator('#tab-inbox').click();
  await page.locator('#section-nav [data-section-page="inbox-connections"]').click();
  await page.waitForURL('**/#/inbox/connections');
  await page.locator('[data-act="conn-config"]').first().click();
  await page.waitForURL('**/#/setup/connectors');await panel('connectors').waitFor();
  assert.equal(await page.locator('#tab-setup.is-on').count(),1);
  await page.locator('#wiki-link').click();
  await page.locator('[data-open-tab="setup/connectors"]').click();
  await page.waitForURL('**/#/setup/connectors');await panel('connectors').waitFor();
  assert.equal(await page.locator('#tab-setup.is-on').count(),1);
  assert.equal(count('/api/connectors/composio/toolkits'),beforeLinks,'Existing Inbox and Wiki links reuse the mounted connector panel');
  checked('Inbox Configure and the Wiki Connectors link still open the nested Setup panel.');
  assert.deepEqual(report.errors,[]);
  assert.deepEqual(report.writes.map(write=>write.path),[
    '/api/connectors/composio/gmail/prefs','/api/connections/gmail','/api/connections/gmail/poll',
    '/api/connectors/composio/telegram/connect']);
}catch(error){
  report.failure=error.stack;await shot('failure.png').catch(()=>{});throw error;
}finally{
  await writeFile(resolve(output,'report.json'),JSON.stringify(report,null,2)+'\n');
  await browser.close();
}
