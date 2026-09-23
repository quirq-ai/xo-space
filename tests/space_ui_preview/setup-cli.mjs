/* Separate CLI access uses fictional browser-owned settings, tokens and
   downloads. No real service setting or local CLI configuration is changed. */
import assert from 'node:assert/strict';
import {mkdir,readFile,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5100';
const endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');
assert.notEqual(endpoint.port,'5002');
const output=resolve(process.argv[2]||'/tmp/space-setup-cli');
await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE
  ?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const browser=await chromium.launch({headless:true,
  executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1100},reducedMotion:'reduce'});
await context.addInitScript(()=>{
  window.fixtureClipboard='';window.fixtureClipboardFails=false;
  Object.defineProperty(navigator,'clipboard',{value:{writeText:async value=>{
    if(window.fixtureClipboardFails)throw new Error('Clipboard unavailable');
    window.fixtureClipboard=value;
  }}});
});
const page=await context.newPage(),errors=[],checks=[],writes=[],downloads=[];
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
const gate=()=>({arrived:deferred(),release:deferred()});
let enabled=false,tokenNumber=0,reads=0,mcpReads=0,failRead=true,failWrite=false,holdRead=null,holdWrite=null;
const status=()=>({enabled,token_configured:enabled,commands:['projects','document','todos','inbox','status'],download_path:'/api/cli-access/client'});
const issue=()=>({...status(),token:'fictional-cli-token-'+(++tokenNumber)});
const send=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
page.on('pageerror',error=>errors.push(error.message));
page.on('console',message=>{
  if(message.type()!=='error')return;
  if(message.location().url.includes('/api/cli-access')&&/403|503/.test(message.text()))return;
  errors.push(message.text());
});
page.on('response',response=>{
  if(response.status()<400)return;
  if(new URL(response.url()).pathname.startsWith('/api/cli-access')&&[403,503].includes(response.status()))return;
  errors.push(response.status()+' '+response.url());
});
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),path=url.pathname,method=request.method();
  assert.equal(url.origin,endpoint.origin,'No external services');
  if(path==='/api/mcp-server'){
    assert.equal(method,'GET','CLI access must never change MCP settings');mcpReads++;
    return send(route,{enabled:false,transport:'streamable-http',endpoint_path:'/mcp',token_configured:false,tools:[]});
  }
  if(path==='/api/cli-access/client'){
    assert.equal(method,'GET');assert.equal(enabled,true);
    assert.equal(url.searchParams.get('fixture_session'),'never-copy-this','Download inherits existing same-origin page authentication');
    assert.doesNotMatch(url.href,/fictional-cli-token/);
    return route.fulfill({contentType:'application/octet-stream',headers:{'Content-Disposition':'attachment; filename="space"'},body:'# Fictional CLI download for browser verification only.\n'});
  }
  if(path==='/api/cli-access'&&method==='GET'){
    reads++;const snapshot=status(),pending=holdRead;holdRead=null;
    if(pending){pending.arrived.resolve();await pending.release.promise;}
    return send(route,failRead?{detail:'CLI settings can only be managed locally.'}:snapshot,failRead?403:200);
  }
  if(path==='/api/cli-access'||path==='/api/cli-access/rotate-token'){
    writes.push({path,method});const pending=holdWrite;holdWrite=null;
    if(pending){pending.arrived.resolve();await pending.release.promise;}
    if(failWrite)return send(route,{detail:'CLI settings could not be saved.'},503);
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
const find=id=>page.locator('#cli-'+id);
async function waitState(value){
  await page.waitForFunction(value=>document.querySelector('#cli-status')?.textContent===value,value);
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
  assert.equal(reads,0,'CLI status loads only when Server opens');
  await section('server');
  await waitState('Unavailable');
  assert.equal(await find('enabled').isDisabled(),true);
  assert.match(await find('error').textContent(),/only be managed locally/);
  failRead=false;await find('retry').click();await waitState('Off');
  assert.equal(await find('connection').isVisible(),false);
  assert.equal(await page.locator('#mcp-enabled').isChecked(),false);
  checked('The Server section opens the separate CLI card, which loads lazily and recovers from local-only errors with MCP disabled.');

  const initial=holdWrite=gate();await find('enabled').click();await initial.arrived.promise;
  assert.equal(await find('enabled').isChecked(),true,'Pending enable remains visually selected');
  assert.equal(await find('enabled').isDisabled(),true);
  initial.release.resolve();await waitState('On');
  assert.equal(await find('token').inputValue(),'fictional-cli-token-1');
  assert.equal(await find('token').getAttribute('type'),'password');
  assert.match(await page.locator('#setup-cli').textContent(),/Python 3\.10\+/);
  assert.match(await page.locator('#setup-cli').textContent(),/does not install a shell command/);
  const command="python3 ./space configure --url '"+origin+"'";
  assert.equal(await find('command').inputValue(),command);
  await find('copy-command').click();assert.equal(await clipboard(),command);
  assert.doesNotMatch(await clipboard(),/fixture_session|never-copy|fictional-cli-token/);
  const href=await find('download').getAttribute('href');
  assert.equal(href,'/api/cli-access/client?fixture_session=never-copy-this');
  const downloadPromise=page.waitForEvent('download');await find('download').click();const download=await downloadPromise;
  const downloadedUrl=new URL(download.url());
  assert.equal(downloadedUrl.origin,origin);assert.equal(downloadedUrl.pathname,'/api/cli-access/client');
  assert.equal(downloadedUrl.searchParams.get('fixture_session'),'never-copy-this');
  assert.doesNotMatch(download.url(),/fictional-cli-token/);downloads.push(downloadedUrl.pathname);
  assert.equal(download.suggestedFilename(),'space');
  assert.match(await readFile(await download.path(),'utf8'),/Fictional CLI download/);
  await find('copy-token').click();assert.equal(await clipboard(),'fictional-cli-token-1');
  await find('show-token').click();assert.equal(await find('token').getAttribute('type'),'text');
  await find('show-token').click();assert.equal(await find('token').getAttribute('type'),'password');
  assert.doesNotMatch(await page.evaluate(()=>JSON.stringify([localStorage,sessionStorage])),/fictional-cli-token/);
  checked('Download preserves page authentication; the copied setup command contains only the quoted origin and token copying never persists it.');

  await page.locator('#setup-refresh').click();await waitState('On');
  assert.equal(await find('token').inputValue(),'fictional-cli-token-1');
  await page.reload({waitUntil:'networkidle'});await waitState('On');
  assert.equal(await find('token').inputValue(),'');assert.equal(await find('copy-token').isDisabled(),true);
  assert.match(await find('token-hint').textContent(),/cannot be shown again/);
  await find('copy-command').click();assert.equal(await clipboard(),command);
  await find('rotate').click();await waitState('On');assert.equal(await find('token').inputValue(),'fictional-cli-token-2');
  checked('Refreshing retains a just-issued token, reload forgets it, and token rotation restores one-time access without changing MCP.');

  failWrite=true;await find('enabled').click();await waitState('On');
  assert.equal(await find('enabled').isChecked(),true);assert.match(await find('error').textContent(),/could not be saved/);
  failWrite=false;await toggle(false);
  assert.equal(await find('token').inputValue(),'');assert.equal(await find('download').getAttribute('href'),null);
  await toggle(true);assert.equal(await find('token').inputValue(),'fictional-cli-token-3');
  const pending=holdWrite=gate();await find('rotate').click();await pending.arrived.promise;
  assert.equal(await find('enabled').isDisabled(),true);assert.equal(await find('download').getAttribute('href'),null);
  await section('workspace');await section('server');pending.release.resolve();await waitState('On');
  assert.equal(await find('token').inputValue(),'fictional-cli-token-4');
  const stale=holdRead=gate();await page.locator('#setup-refresh').click();await stale.arrived.promise;
  enabled=false;await section('workspace');await section('server');stale.release.resolve();await waitState('Off');
  assert.equal(await find('token').inputValue(),'');
  await toggle(true);
  assert.equal(await page.locator('#mcp-enabled').isChecked(),false);assert.ok(mcpReads>0);
  checked('Failed writes restore confirmed state; disabling clears tokens and navigation during pending requests preserves the newest CLI state.');

  await page.evaluate(()=>{window.fixtureClipboardFails=true;});await find('copy-command').click();
  assert.equal(await find('command').evaluate(el=>document.activeElement===el&&el.selectionStart===0&&el.selectionEnd===el.value.length),true);
  await page.evaluate(()=>{window.fixtureClipboardFails=false;});
  const examples=page.locator('.setup-cli-examples');await examples.locator('summary').click();
  assert.match(await examples.textContent(),/python3 \.\/space document PROJECT PLAN\.md/);
  assert.match(await examples.textContent(),/python3 \.\/space inbox --json/);
  for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:1100});await page.locator('#setup-cli').scrollIntoViewIfNeeded();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'No horizontal page overflow at '+width);
    assert.equal(await page.locator('#setup-cli').evaluate(el=>el.scrollWidth<=el.clientWidth),true,'CLI card fits at '+width);
    await page.screenshot({path:resolve(output,'cli-'+width+'.png')});
  }
  checked('Clipboard fallback, terminal examples and desktop/390px/320px layout work while MCP remains disabled.');
  assert.deepEqual(errors,[]);
  await writeFile(resolve(output,'report.json'),JSON.stringify({checks,writes,downloads,reads,mcpReads,errors},null,2));
}finally{await browser.close();}
