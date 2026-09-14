/* Setup identity statuses use fictional browser responses. No environment
   dump, browser-session mint, connector operation or service write is allowed. */
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5100';
const endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');
assert.notEqual(endpoint.port,'5002');
assert.notEqual(endpoint.port,'5112','Preserve the interactive Commands server');
const output=resolve(process.argv[2]||'/tmp/space-setup-identity-status');
await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE
  ?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const browser=await chromium.launch({headless:true,
  executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
const page=await context.newPage();
const report={origin,checks:[],requests:[],errors:[],screenshots:[],layouts:[],statusReads:0,expectedFailures:0};
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
const gate=()=>({arrived:deferred(),release:deferred()});
const connected=()=>({checked_at:'2026-09-14T10:00:00Z',
  space:{status:'configured',id:'space-fixture-123',label:'Review <workspace>',owner:'demo-owner'},
  xo:{status:'connected',user_id:'xo-user-123'},
  github:{status:'connected',username:'demo-developer',source:'connector'},
  token:'fixture-private-value-that-must-never-render'});
let payload=connected(),httpStatus=200,holdStatus=null;
const send=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
page.on('pageerror',error=>report.errors.push(error.message));
page.on('console',message=>{
  if(message.type()!=='error')return;
  if(message.location().url===origin+'/space/setup/status'&&message.text().includes('503'))return;
  report.errors.push(message.text());
});
page.on('response',response=>{
  if(response.status()<400)return;
  if(new URL(response.url()).pathname==='/space/setup/status'&&response.status()===503)report.expectedFailures++;
  else report.errors.push(response.status()+' '+response.url());
});
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),path=url.pathname;
  report.requests.push({path,method:request.method()});
  const forbidden=url.origin!==endpoint.origin||request.method()!=='GET'
    ||path==='/api/secrets/env'||path.startsWith('/xo-auth/')
    ||path.startsWith('/api/connectors/')||path.startsWith('/api/connections');
  if(forbidden){
    report.errors.push('Blocked unexpected request: '+request.method()+' '+request.url());
    return send(route,{error:'Blocked by the identity fixture'});
  }
  if(path==='/space/setup/status'){
    report.statusReads++;
    const snapshot=structuredClone(payload),status=httpStatus,pending=holdStatus;holdStatus=null;
    if(pending){pending.arrived.resolve();await pending.release.promise;}
    return send(route,snapshot,status);
  }
  return route.continue();
});
const card=page.locator('#setup-identity');
const status=kind=>card.locator('[data-setup-identity="'+kind+'"] .setup-identity-status');
const detail=kind=>card.locator('[data-setup-identity="'+kind+'"] .setup-identity-detail');
async function waitStatus(xo,github){
  await page.waitForFunction(({xo,github})=>
    document.querySelector('[data-setup-identity="xo"] .setup-identity-status')?.textContent===xo
    &&document.querySelector('[data-setup-identity="github"] .setup-identity-status')?.textContent===github,{xo,github});
}
async function startRefresh(){
  await page.waitForFunction(()=>!document.querySelector('#setup-refresh').disabled);
  await page.locator('#setup-refresh').click();
}
async function refresh(xo,github){
  const response=page.waitForResponse(response=>new URL(response.url()).pathname==='/space/setup/status');
  await startRefresh();await response;await waitStatus(xo,github);
}
function checked(text){report.checks.push(text);console.log(text);}

try{
  await page.goto(origin+'/space/#/setup',{waitUntil:'networkidle'});
  await waitStatus('Connected','Connected');
  assert.equal(await page.locator('#tab-setup.is-on').count(),1);
  assert.equal(await page.locator('#view-setup.is-active').count(),1);
  assert.deepEqual(await card.locator('.setup-identity-space dd').allTextContents(),
    ['space-fixture-123','Review <workspace>','demo-owner']);
  assert.match(await detail('xo').textContent(),/User ID: xo-user-123/);
  assert.match(await detail('github').textContent(),/@demo-developer/);
  assert.match(await detail('github').textContent(),/Saved connection/i);
  assert.equal(await card.locator('.is-good').count(),2);
  assert.equal(await card.locator('img,script').count(),0);
  assert.doesNotMatch(await card.textContent(),/fixture-private-value/);
  checked('Workspace metadata, verified XO user and verified GitHub username render safely without credential values.');

  payload.github.source='env';await refresh('Connected','Connected');
  assert.match(await detail('github').textContent(),/Environment.*GITHUB_PAT/i);
  assert.doesNotMatch(await detail('github').textContent(),/Saved connection|connector credential/i);
  payload={space:{status:'not_configured'},xo:{status:'not_configured'},github:{status:'not_configured'}};
  await refresh('Not configured','Not configured');
  assert.deepEqual(await card.locator('.setup-identity-space dd').allTextContents(),['Not configured']);
  assert.match(await detail('xo').textContent(),/No XO account credential/);
  assert.match(await detail('github').textContent(),/No GitHub credential/);
  assert.equal(await card.locator('.is-good').count(),0);
  assert.doesNotMatch(await card.textContent(),/xo-user-123|demo-developer|demo-owner|space-fixture-123/);
  checked('Disconnected accounts and environment-versus-connector GitHub sources are shown distinctly; former identities are cleared.');

  payload={space:{status:'configured',id:'space-fixture-123'},
    xo:{status:'rejected',user_id:'unverified-xo'},github:{status:'rejected',username:'unverified-github',source:'env'}};
  await refresh('Authentication rejected','Authentication rejected');
  assert.equal(await card.locator('.is-error').count(),2);
  assert.match(await detail('xo').textContent(),/Check the saved credential/);
  assert.doesNotMatch(await card.textContent(),/unverified-xo|unverified-github/);
  payload={space:{status:'unavailable'},xo:{status:'unavailable'},github:{status:'unavailable',source:'connector'}};
  await refresh('Unable to verify','Unable to verify');
  assert.deepEqual(await card.locator('.setup-identity-space dd').allTextContents(),['Unavailable']);
  assert.equal(await card.locator('.is-error,.is-good').count(),0,'Unknown availability is neither verified nor rejected authentication');
  payload={space:{status:'configured',id:''},xo:{status:'connected',user_id:null},github:{status:'connected',username:' '}};
  await refresh('Unable to verify','Unable to verify');
  payload={detail:'fixture-private-error-value'};httpStatus=503;
  await refresh('Unable to verify','Unable to verify');
  assert.doesNotMatch(await card.textContent(),/fixture-private-error-value/);
  httpStatus=200;payload=connected();await refresh('Connected','Connected');
  checked('Rejected, unavailable, malformed success and failed requests never display an unverified account or raw service error.');

  // An explicit refresh clears the previous verified identity immediately.
  payload=connected();payload.xo.user_id='xo-stale';payload.github.username='stale-github';
  const old=holdStatus=gate();await startRefresh();await old.arrived.promise;
  await waitStatus('Checking…','Checking…');
  assert.doesNotMatch(await card.textContent(),/xo-user-123|demo-developer|Connected/);
  await page.evaluate(()=>{
    window.identityPaints=[];
    window.identityObserver=new MutationObserver(()=>window.identityPaints.push(document.querySelector('#setup-identity').textContent));
    window.identityObserver.observe(document.querySelector('#setup-identity'),{childList:true,subtree:true,characterData:true});
  });
  const reads=report.statusReads;
  payload=connected();payload.xo.user_id='xo-latest';payload.github.username='latest-github';
  const fresh=holdStatus=gate();await startRefresh();
  assert.equal(report.statusReads,reads,'Repeated refresh waits for the pending request before fetching fresh status');
  old.release.resolve();await fresh.arrived.promise;
  await waitStatus('Checking…','Checking…');
  assert.doesNotMatch(await card.textContent(),/xo-stale|stale-github/);
  fresh.release.resolve();await waitStatus('Connected','Connected');
  assert.match(await detail('xo').textContent(),/xo-latest/);
  assert.match(await detail('github').textContent(),/@latest-github/);
  const paints=await page.evaluate(()=>{window.identityObserver.disconnect();return window.identityPaints;});
  assert.ok(paints.every(value=>!value.includes('xo-stale')&&!value.includes('stale-github')),'A superseded identity never flashes in the DOM');
  assert.equal(report.statusReads,reads+1);
  checked('Slow refresh clears old identity; a queued refresh ignores the late stale response and paints only the newer verification.');

  payload=connected();payload.space.id='space-with-a-long-id-0123456789-abcdefghijklmnopqrstuvwxyz-0123456789';
  payload.github.username='long-fictional-github-user-name';await refresh('Connected','Connected');
  for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:1000});
    await page.waitForFunction(()=>{
      const tab=document.querySelector('#tab-setup'),tabs=tab.parentElement;
      const active=tab.getBoundingClientRect(),strip=tabs.getBoundingClientRect();
      return active.left>=strip.left-1&&active.right<=strip.right+1;
    });
    await card.scrollIntoViewIfNeeded();
    const tabBounds=await page.evaluate(()=>{
      const tab=document.querySelector('#tab-setup'),strip=tab.parentElement;
      const a=tab.getBoundingClientRect(),s=strip.getBoundingClientRect();
      return{width:innerWidth,left:a.left,right:a.right,stripLeft:s.left,stripRight:s.right,scrollLeft:strip.scrollLeft};
    });
    report.layouts.push(tabBounds);
    assert.ok(tabBounds.left>=tabBounds.stripLeft-1&&tabBounds.right<=tabBounds.stripRight+1,
      width+'px active Setup tab remains visible while viewing workspace identity');
    const bounds=await card.locator('dt,dd,h4,p,.setup-identity-status').evaluateAll(nodes=>nodes.map(node=>{
      const r=node.getBoundingClientRect();return{left:r.left,right:r.right};
    }));
    assert.ok(bounds.every(bound=>bound.left>=-1&&bound.right<=width+1),width+'px identity content fits');
    const name='setup-identity-'+width+'.png';
    await page.screenshot({path:resolve(output,name)});report.screenshots.push(name);
  }
  checked('Long identity values fit at 1440px, 390px and 320px.');
  assert.equal(report.expectedFailures,1);
  assert.deepEqual(report.errors,[]);
  assert.equal(report.requests.some(request=>request.method!=='GET'),false);
  assert.equal(report.requests.some(request=>request.path==='/api/secrets/env'||request.path.startsWith('/xo-auth/')),false);
}catch(error){
  report.failure=error.stack;await page.screenshot({path:resolve(output,'failure.png')}).catch(()=>{});throw error;
}finally{
  await writeFile(resolve(output,'report.json'),JSON.stringify(report,null,2)+'\n');
  await browser.close();
}
