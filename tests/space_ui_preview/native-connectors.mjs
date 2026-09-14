/* Native connector flow checks. Every provider/session request and mutation is
   intercepted in isolated browser memory; no real credential or login is used. */
import assert from 'node:assert/strict';
import {openProjectList} from './routes.mjs';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5100';
const endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');
assert.ok(!['5002','5112'].includes(endpoint.port),'Use the read-only fixture, preserving user settings and commands');
const output=resolve(process.argv[2]||'/tmp/space-native-connectors');
await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE
  ?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const browser=await chromium.launch({headless:true,
  executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const report={origin,checks:[],requests:[],writes:[],errors:[],screenshots:[]};
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
const gate=()=>({arrived:deferred(),release:deferred()});
let holdToken=null,holdDevicePoll=null;
const state={github:false,vercel:false,magicInstalled:false,magicLoggedIn:false,
  driveComplete:false,remotes:[],vercelLink:'javascript:alert(1)',githubPolls:0};
const json=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
function checked(text){report.checks.push(text);console.log(text);}
async function fixture(signedIn=true){
  const context=await browser.newContext({viewport:{width:1440,height:1100},reducedMotion:'reduce'});
  await context.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url()),path=url.pathname,method=request.method();
    if(url.origin!==origin){report.errors.push('Unexpected external request: '+request.url());return route.abort();}
    const related=path==='/xo-auth/session/self'||path.startsWith('/api/connectors/')||path.startsWith('/api/connections');
    if(related)report.requests.push({path,method});
    if(method!=='GET')report.writes.push({path,method});
    if(path==='/xo-auth/session/self')return json(route,signedIn?{session_id:'fictional-native-session'}:{detail:'No session'},signedIn?200:403);
    if(path==='/api/connectors/composio/toolkits')return json(route,{toolkits:[]});
    if(path==='/api/connections')return json(route,{signed_in:signedIn,poller_enabled:false,connections:[]});
    if(path==='/api/connectors/github/status')return json(route,state.github?{status:'connected',username:'fixture-developer'}:{status:'needs_auth'});
    if(path==='/api/connectors/vercel/status')return json(route,state.vercel?{status:'connected',username:'fixture-builder'}:{status:'needs_auth'});
    if(path==='/api/connectors/magicpath/status')return json(route,{cli_installed:state.magicInstalled,skill_installed:state.magicInstalled,
      logged_in:state.magicLoggedIn,user:state.magicLoggedIn?{email:'design@example.test'}:null});
    if(path==='/api/connectors/github/token'){
      assert.equal(method,'POST');
      if(request.postDataJSON().token==='fictional-rejected-token')return json(route,{detail:'RAW fictional-rejected-token must never render'},401);
      assert.deepEqual(request.postDataJSON(),{token:'fictional-valid-token'});
      const pending=holdToken;holdToken=null;if(pending){pending.arrived.resolve();await pending.release.promise;}
      state.github=true;return json(route,{status:'connected',username:'fixture-developer'});
    }
    if(path==='/api/connectors/github/disconnect'){assert.equal(method,'POST');state.github=false;return json(route,{status:'needs_auth'});}
    if(path==='/api/connectors/github/cli/start'){
      assert.equal(method,'POST');assert.deepEqual(request.postDataJSON(),{});
      return json(route,{session_id:'fixture-github-login',user_code:'TEST-CODE',verification_uri:'https://github.com/login/device'});
    }
    if(path==='/api/connectors/github/cli/poll'){
      assert.equal(method,'POST');assert.deepEqual(request.postDataJSON(),{session_id:'fixture-github-login'});
      state.githubPolls++;
      const pending=holdDevicePoll;holdDevicePoll=null;if(pending){pending.arrived.resolve();await pending.release.promise;}
      return json(route,{status:state.github?'connected':'pending'});
    }
    if(path==='/api/connectors/github/cli/cancel'){
      assert.equal(method,'POST');assert.deepEqual(request.postDataJSON(),{session_id:'fixture-github-login'});
      return json(route,{status:'cancelled'});
    }
    if(path==='/api/connectors/magicpath/setup'){
      assert.equal(method,'POST');state.magicInstalled=true;return json(route,{ok:true});
    }
    if(path==='/api/connectors/magicpath/login'){
      assert.equal(method,'POST');const body=request.postDataJSON();
      if(!body.code)return json(route,{login_url:'https://www.magicpath.ai/auth/cli',cli_installed:true});
      assert.deepEqual(body,{code:'fictional-magic-code'});state.magicLoggedIn=true;
      return json(route,{ok:true,user:{email:'design@example.test'}});
    }
    if(path==='/api/connectors/magicpath/logout'){
      assert.equal(method,'POST');state.magicLoggedIn=false;return json(route,{ok:true});
    }
    if(path==='/api/connectors/vercel/oauth/start'){
      assert.equal(method,'GET');return json(route,{auth_url:state.vercelLink,state:'fixture-state',reachable_callback:false,
        redirect_uri:'http://localhost:5002/api/connectors/vercel/oauth/callback'});
    }
    if(path==='/api/connectors/vercel/oauth/exchange'){
      assert.equal(method,'POST');assert.deepEqual(request.postDataJSON(),{callback_url:'http://localhost:5002/callback?code=fictional-vercel-code'});
      state.vercel=true;return json(route,{status:'connected',username:'fixture-builder'});
    }
    const remote=path.match(/^\/api\/connectors\/(gdrive|onedrive)\/remotes(?:\/([^/]+))?$/);
    if(remote){
      const [,id,name]=remote;
      if(method==='GET')return json(route,{remotes:id==='gdrive'?state.remotes:[]});
      if(method==='DELETE'){
        assert.equal(id,'gdrive');assert.equal(name,'team-files');state.remotes=[];return route.fulfill({status:204});
      }
      assert.equal(method,'POST');assert.deepEqual(request.postDataJSON(),{name:id==='gdrive'?'team-files':'work-files'});
      return json(route,{session_id:'fixture-'+id+'-login',status:'pending'},202);
    }
    const session=path.match(/^\/api\/connectors\/(gdrive|onedrive)\/sessions\/fixture-(gdrive|onedrive)-login(?:\/(submit|cancel))?$/);
    if(session){
      const [,id,sessionId,action]=session;assert.equal(id,sessionId);
      if(action==='submit'){
        assert.equal(method,'POST');assert.equal(id,'gdrive');
        assert.deepEqual(request.postDataJSON(),{code:'http://localhost/?code=fictional-drive-code'});
        state.driveComplete=true;state.remotes=[{name:'team-files',complete:true,type:'drive'}];return json(route,{ok:true});
      }
      if(action==='cancel'){assert.equal(method,'POST');return json(route,{status:'cancelled'});}
      assert.equal(method,'GET');return json(route,state.driveComplete&&id==='gdrive'?{status:'completed'}:
        {status:'awaiting_oauth',auth_url:'https://accounts.example.test/authorize',needs_manual_code:true});
    }
    if(related||method!=='GET'){
      report.errors.push('Blocked unexpected request: '+method+' '+path);return json(route,{error:'Unexpected request blocked'});
    }
    return route.continue();
  });
  const page=await context.newPage();
  page.on('pageerror',error=>report.errors.push(error.message));
  page.on('dialog',dialog=>dialog.accept());
  await page.goto(origin+'/space/#/connectors',{waitUntil:'networkidle'});
  await page.locator('[data-native-connector="github"] .conn-state').filter({hasText:/Not connected|Connected/}).waitFor();
  await page.waitForFunction(()=>[...document.querySelectorAll('[data-native-connector] .conn-state')].every(node=>!node.textContent.includes('Checking')));
  return{context,page};
}
const native=(page,id)=>page.locator('[data-native-connector="'+id+'"]');
const act=(page,id,name)=>native(page,id).locator('[data-native-action="'+name+'"]');
async function submit(page,id,kind){await native(page,id).locator('form[data-native-form="'+kind+'"] button[type="submit"]').click();}
async function connected(page,id){await native(page,id).locator('.conn-state').filter({hasText:/Connected|Configured/}).waitFor();}
async function shot(page,name){await page.screenshot({path:resolve(output,name),fullPage:true});report.screenshots.push(name);}
let context,page;
try{
  ({context,page}=await fixture());
  assert.equal(await page.locator('[data-native-connector]').count(),5);
  assert.deepEqual(report.writes,[],'Opening native cards makes no provider writes');
  checked('GitHub, MagicPath, Vercel, Google Drive and OneDrive status cards load without provider writes.');

  await act(page,'github','open').click();
  const token=native(page,'github').locator('input[name="token"]');
  assert.equal(await token.getAttribute('type'),'password');
  await token.fill('fictional-rejected-token');await submit(page,'github','token');
  await native(page,'github').locator('[role="alert"]').filter({hasText:'Authorization was rejected'}).waitFor();
  assert.doesNotMatch(await page.locator('body').innerText(),/RAW|fictional-rejected-token/);
  assert.equal(await token.inputValue(),'fictional-rejected-token','Failed save preserves editable draft');
  await token.fill('fictional-valid-token');const tokenNode=await token.elementHandle();
  await page.locator('#view-search').fill('MagicPath');
  await page.locator('#conn-refresh').click();
  await page.locator('#view-search').fill('GitHub');
  assert.equal(await tokenNode.evaluate(node=>node.isConnected),true);
  assert.equal(await token.inputValue(),'fictional-valid-token');
  const pending=holdToken=gate();await submit(page,'github','token');await pending.arrived.promise;
  assert.equal(await token.isDisabled(),true);
  await page.locator('#setup-nav [data-setup-go="workspace"]').click();
  await openProjectList(page);await page.waitForURL('**/#/projects/list');
  pending.release.resolve();
  await page.waitForFunction(()=>document.querySelector('[data-native-connector="github"] .conn-state').textContent==='Connected');
  assert.equal(new URL(page.url()).hash,'#/projects/list','Pending save does not take over navigation');
  await page.locator('#tab-setup').click();await page.locator('#setup-nav [data-setup-go="connectors"]').click();
  await page.locator('#view-search').fill('');
  assert.equal(await token.inputValue(),'','Successful save clears token from DOM');
  assert.match(await native(page,'github').textContent(),/@fixture-developer/);
  checked('Token errors are sanitized; filtering, Refresh and navigation preserve drafts/pending saves; success clears credentials.');

  await act(page,'github','disconnect').click();
  await native(page,'github').locator('.conn-state').filter({hasText:'Not connected'}).waitFor();
  await act(page,'github','open').click();
  const device=holdDevicePoll=gate();await act(page,'github','browser').click();await device.arrived.promise;
  assert.equal(await native(page,'github').locator('[data-native-link]').getAttribute('href'),'https://github.com/login/device');
  assert.match(await native(page,'github').locator('[data-native-code]').textContent(),/TEST-CODE/);
  await page.locator('#view-search').fill('magicpath');await page.locator('#conn-refresh').click();
  await page.locator('#view-search').fill('github');
  assert.match(await native(page,'github').locator('.conn-state').textContent(),/pending/);
  await act(page,'github','cancel').click();
  device.release.resolve();
  await native(page,'github').locator('.conn-state').filter({hasText:'Not connected'}).waitFor();
  assert.equal(await native(page,'github').locator('[data-native-link]').isVisible(),false);
  checked('GitHub device sign-in survives Refresh/filtering; cancel clears the pending flow and ignores late polling.');

  await page.locator('#view-search').fill('');
  await act(page,'magicpath','setup').click();
  await native(page,'magicpath').locator('.conn-state').filter({hasText:'Sign-in not verified'}).waitFor();
  await act(page,'magicpath','open').click();await act(page,'magicpath','browser').click();
  await native(page,'magicpath').locator('form[data-native-form="code"]').waitFor();
  await native(page,'magicpath').locator('input[name="code"]').fill('fictional-magic-code');
  await submit(page,'magicpath','code');await connected(page,'magicpath');
  assert.match(await native(page,'magicpath').textContent(),/design@example.test/);
  assert.equal(await native(page,'magicpath').locator('input[name="code"]').inputValue(),'');
  await act(page,'magicpath','disconnect').click();
  await native(page,'magicpath').locator('.conn-state').filter({hasText:'Sign-in not verified'}).waitFor();
  checked('MagicPath installation, code sign-in and logout use existing fixture endpoints; completed code is cleared.');

  await act(page,'vercel','open').click();await act(page,'vercel','browser').click();
  await native(page,'vercel').locator('[role="alert"]').filter({hasText:'invalid sign-in link'}).waitFor();
  assert.equal(await native(page,'vercel').locator('[data-native-link]').getAttribute('href'),null);
  state.vercelLink='https://vercel.com/oauth/authorize';
  await act(page,'vercel','browser').click();
  await native(page,'vercel').locator('form[data-native-form="code"]').waitFor();
  await native(page,'vercel').locator('input[name="code"]').fill('http://localhost:5002/callback?code=fictional-vercel-code');
  await submit(page,'vercel','code');await connected(page,'vercel');
  assert.equal(await native(page,'vercel').locator('input[name="code"]').inputValue(),'');
  await act(page,'vercel','open').click();
  assert.equal(await act(page,'vercel','browser').isDisabled(),true,'An existing Vercel account cannot falsely complete a new OAuth flow');
  assert.match(await act(page,'vercel','browser').textContent(),/Disconnect before browser sign-in/);
  checked('Vercel rejects unsafe sign-in URLs and completes the OAuth redirect flow without exposing callback data.');

  for(const id of ['gdrive','onedrive']){
    await act(page,id,'open').click();
    await native(page,id).locator('input[name="name"]').fill(id==='gdrive'?'team-files':'work-files');
    await submit(page,id,'remote');
    await native(page,id).locator('form[data-native-form="code"]').waitFor();
    if(id==='gdrive'){
      await native(page,id).locator('input[name="code"]').fill('http://localhost/?code=fictional-drive-code');
      const codeNode=await native(page,id).locator('input[name="code"]').elementHandle();
      await page.locator('#view-search').fill('magicpath');await page.locator('#conn-refresh').click();
      await page.locator('#view-search').fill('Google Drive');
      assert.equal(await codeNode.evaluate(node=>node.isConnected),true,'Drive redirect form remains mounted during refresh');
      assert.equal(await native(page,id).locator('input[name="code"]').inputValue(),'http://localhost/?code=fictional-drive-code');
      assert.match(await native(page,id).locator('.conn-state').textContent(),/pending/);
      await submit(page,id,'code');await connected(page,id);
      await page.locator('#view-search').fill('');
      assert.match(await native(page,id).textContent(),/team-files/);
    }else{
      await act(page,id,'cancel').click();
      await native(page,id).locator('.conn-state').filter({hasText:'Not configured'}).waitFor();
    }
  }
  await act(page,'gdrive','remove').click();
  await native(page,'gdrive').locator('.conn-native-remote').waitFor({state:'detached'});
  assert.match(await native(page,'gdrive').locator('.conn-state').textContent(),/Not configured/);
  checked('Drive flows add an account, submit OAuth redirects, cancel pending sign-in and accept 204 removal responses.');

  await act(page,'gdrive','open').click();
  await native(page,'gdrive').locator('input[name="name"]').fill('a-new-account-draft');
  for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:1100});
    await page.locator('#view-search').fill('');
    const bounds=await page.locator('[data-native-connector], [data-native-connector] input, [data-native-connector] button').evaluateAll(nodes=>nodes.filter(node=>node.getClientRects().length).map(node=>{
      const r=node.getBoundingClientRect();return{tag:node.tagName,left:r.left,right:r.right};
    }));
    assert.ok(bounds.every(r=>r.left>=-1&&r.right<=width+1),width+'px native cards/forms fit: '+JSON.stringify(bounds));
    await shot(page,'native-connectors-'+width+'.png');
    await native(page,'gdrive').locator('input[name="name"]').scrollIntoViewIfNeeded();
    await shot(page,'native-drive-form-'+width+'.png');
  }
  checked('Native cards and expanded forms fit at 1440px, 390px and 320px.');
  await context.close();

  const before=report.writes.length;
  ({context,page}=await fixture(false));
  assert.equal(await page.locator('[data-native-connector]').count(),5);
  await native(page,'github').waitFor();
  assert.equal(await act(page,'github','open').isEnabled(),true);
  await page.locator('#view-search').fill('MagicPath');
  assert.equal(await native(page,'magicpath').isVisible(),true);
  assert.equal(await native(page,'github').isVisible(),false);
  await page.locator('#view-search').fill('no-connectors-match-this-fixture');
  await page.locator('#conn-no-match').waitFor();
  assert.equal(await page.locator('[data-native-connector]:visible').count(),0);
  await page.locator('#view-search-clear').click();
  assert.equal(await page.locator('[data-native-connector]:visible').count(),5);
  assert.equal(report.writes.length,before,'Unavailable XO session never initiates native auth/install');
  checked('All native connectors remain available and searchable when XO account sign-in is unavailable.');
  assert.deepEqual(report.errors,[]);
}catch(error){report.failure=error.stack;if(page&&!page.isClosed())await shot(page,'failure.png').catch(()=>{});throw error;}
finally{await writeFile(resolve(output,'report.json'),JSON.stringify(report,null,2)+'\n');await browser.close();}
