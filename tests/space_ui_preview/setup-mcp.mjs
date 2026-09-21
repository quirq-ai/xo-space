/* MCP settings use fictional in-memory responses. No server settings, real
   credentials, external clients or installation data are touched. */
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5100';
const endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');
assert.notEqual(endpoint.port,'5002');
const output=resolve(process.argv[2]||'/tmp/space-setup-mcp');
await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE
  ?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const browser=await chromium.launch({headless:true,
  executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
await context.addInitScript(()=>{
  window.fixtureClipboard='';window.fixtureClipboardFails=false;
  Object.defineProperty(navigator,'clipboard',{value:{writeText:async value=>{
    if(window.fixtureClipboardFails)throw new Error('Clipboard unavailable');
    window.fixtureClipboard=value;
  }}});
});
const page=await context.newPage(),errors=[],checks=[],writes=[];
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
const gate=()=>({arrived:deferred(),release:deferred()});
let enabled=false,tokenNumber=0,reads=0,failRead=true,failWrite=false,holdRead=null,holdWrite=null;
const status=()=>({enabled,transport:'streamable-http',endpoint_path:'/mcp',token_configured:enabled,
  tools:[{name:'space_list_projects',description:'List projects.'},{name:'space_read_project_document',description:'Read selected documents.'},
    {name:'space_list_todos',description:'List tasks.'},{name:'space_list_inbox',description:'List Inbox items.'}]});
const issue=()=>({...status(),token:'fictional-mcp-token-'+(++tokenNumber)});
const send=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
page.on('pageerror',error=>errors.push(error.message));
page.on('console',message=>{
  if(message.type()!=='error')return;
  if(message.location().url.includes('/api/mcp-server')&&/403|503/.test(message.text()))return;
  errors.push(message.text());
});
page.on('response',response=>{
  if(response.status()<400)return;
  if(new URL(response.url()).pathname.startsWith('/api/mcp-server')&&[403,503].includes(response.status()))return;
  errors.push(response.status()+' '+response.url());
});
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),path=url.pathname,method=request.method();
  assert.equal(url.origin,endpoint.origin,'No external services');
  if(path==='/api/mcp-server'&&method==='GET'){
    reads++;const snapshot=status(),pending=holdRead;holdRead=null;
    if(pending){pending.arrived.resolve();await pending.release.promise;}
    return send(route,failRead?{detail:'MCP settings can only be managed locally.'}:snapshot,failRead?403:200);
  }
  if(path==='/api/mcp-server'||path==='/api/mcp-server/rotate-token'){
    writes.push({path,method});const pending=holdWrite;holdWrite=null;
    if(pending){pending.arrived.resolve();await pending.release.promise;}
    if(failWrite)return send(route,{detail:'MCP settings could not be saved.'},503);
    if(path.endsWith('/rotate-token')){
      assert.equal(method,'POST');assert.equal(enabled,true);return send(route,issue());
    }
    assert.equal(method,'PUT');const body=request.postDataJSON();assert.deepEqual(Object.keys(body),['enabled']);
    const newlyEnabled=body.enabled&&!enabled;enabled=body.enabled;
    return send(route,newlyEnabled?issue():status());
  }
  assert.equal(method,'GET','All other service writes are blocked');
  return route.continue();
});
const find=id=>page.locator('#mcp-'+id);
async function waitState(value){
  await page.waitForFunction(value=>document.querySelector('#mcp-status')?.textContent===value,value);
}
async function section(id){
  await page.evaluate(id=>{location.hash='#/setup/'+id;},id);
  await page.locator('#setup-panel-'+id).waitFor({state:'visible'});
}
async function toggle(on){
  assert.equal(await find('enabled').isChecked(),!on);await find('enabled').click();await waitState(on?'On':'Off');
}
async function clipboard(){return page.evaluate(()=>window.fixtureClipboard);}
function checked(text){checks.push(text);console.log(text);}

try{
  await page.goto(origin+'/space/?fixture_session=never-copy-this#/setup/workspace',{waitUntil:'networkidle'});
  assert.equal(reads,0,'MCP status loads only when Server opens');
  await section('server');await waitState('Unavailable');
  assert.equal(await find('enabled').isDisabled(),true);
  assert.match(await find('error').textContent(),/only be managed locally/);
  assert.equal(await find('connection').isVisible(),false);
  failRead=false;await find('retry').click();await waitState('Off');
  assert.equal(await find('enabled').isChecked(),false);
  assert.match(await page.locator('#mcp-description').textContent(),/project documents, tasks, and Inbox/);
  checked('The Server section loads MCP settings lazily, reports local-only errors and retries safely.');

  await toggle(true);
  assert.equal(await find('url').inputValue(),origin+'/mcp');
  assert.equal(await find('token').inputValue(),'fictional-mcp-token-1');
  assert.equal(await find('token').getAttribute('type'),'password');
  await find('copy-url').click();assert.equal(await clipboard(),origin+'/mcp');
  await find('copy-token').click();assert.equal(await clipboard(),'fictional-mcp-token-1');
  await find('show-token').click();assert.equal(await find('token').getAttribute('type'),'text');
  await find('show-token').click();assert.equal(await find('token').getAttribute('type'),'password');
  await find('copy-config').click();
  assert.deepEqual(JSON.parse(await clipboard()),{mcpServers:{space:{type:'http',url:origin+'/mcp',headers:{Authorization:'Bearer fictional-mcp-token-1'}}}});
  assert.doesNotMatch(await clipboard(),/fixture_session|never-copy/);
  assert.doesNotMatch(await page.evaluate(()=>JSON.stringify([localStorage,sessionStorage])),/fictional-mcp-token/);
  checked('Enabling applies immediately; URL, masked token and client config copy correctly without page credentials or persistent storage.');

  await page.locator('#setup-refresh').click();await waitState('On');
  assert.equal(await find('token').inputValue(),'fictional-mcp-token-1','Status refresh retains the just-issued in-memory token');
  await page.reload({waitUntil:'networkidle'});await waitState('On');
  assert.equal(await find('token').inputValue(),'');
  assert.equal(await find('copy-token').isDisabled(),true);
  assert.match(await find('token-hint').textContent(),/cannot be shown again/);
  await find('copy-config').click();assert.equal(JSON.parse(await clipboard()).mcpServers.space.headers.Authorization,'Bearer YOUR_ACCESS_TOKEN');
  await find('rotate').click();await waitState('On');
  assert.equal(await find('token').inputValue(),'fictional-mcp-token-2');
  checked('Reloading forgets the token; copied config uses an explicit placeholder until a new token is generated.');

  failWrite=true;await find('enabled').click();await waitState('On');
  assert.equal(await find('enabled').isChecked(),true,'Failed disable restores confirmed server state');
  assert.match(await find('error').textContent(),/could not be saved/);
  assert.equal(await find('token').inputValue(),'fictional-mcp-token-2');
  failWrite=false;await toggle(false);
  assert.equal(await find('connection').isVisible(),false);
  assert.equal(await find('token').inputValue(),'');
  await toggle(true);assert.equal(await find('token').inputValue(),'fictional-mcp-token-3');
  checked('Failed writes show an actionable error; disabling clears copied credentials and re-enabling receives a new token.');

  const pending=holdWrite=gate();await find('rotate').click();await pending.arrived.promise;
  assert.equal(await find('rotate').isDisabled(),true);
  assert.equal(await find('enabled').isDisabled(),true);
  await section('workspace');await section('server');
  pending.release.resolve();await waitState('On');
  assert.equal(await find('token').inputValue(),'fictional-mcp-token-4','A queued status refresh cannot discard the newly issued token');
  const stale=holdRead=gate();await page.locator('#setup-refresh').click();await stale.arrived.promise;
  assert.equal(await find('enabled').isDisabled(),true,'No writes use state during a pending status read');
  enabled=false;await section('workspace');await section('server');
  stale.release.resolve();await waitState('Off');
  assert.equal(await find('token').inputValue(),'','A fresh read after the held response clears a revoked token');
  await toggle(true);
  checked('Navigation during pending reads and writes preserves the newest server state and prevents duplicate submissions.');

  await page.evaluate(()=>{window.fixtureClipboardFails=true;});
  await find('copy-url').click();
  assert.equal(await find('url').evaluate(el=>document.activeElement===el&&el.selectionStart===0&&el.selectionEnd===el.value.length),true);
  await find('copy-config').click();assert.match(await find('error').textContent(),/Clipboard access is unavailable/);
  await page.evaluate(()=>{window.fixtureClipboardFails=false;});
  await page.locator('#setup-refresh').click();await waitState('On');
  for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:1000});
    await page.locator('#setup-mcp').scrollIntoViewIfNeeded();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'No horizontal page overflow at '+width);
    assert.equal(await page.locator('#setup-mcp').evaluate(el=>el.scrollWidth<=el.clientWidth),true,'MCP card fits at '+width);
    await page.screenshot({path:resolve(output,'mcp-'+width+'.png')});
  }
  checked('Clipboard failures remain actionable and the card fits desktop, 390px and 320px layouts.');
  assert.deepEqual(errors,[]);
  await writeFile(resolve(output,'report.json'),JSON.stringify({checks,writes,reads,errors},null,2));
}finally{await browser.close();}
