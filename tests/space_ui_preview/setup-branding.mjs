/* Workspace branding uses fictional, browser-owned settings and image bytes.
   All writes are intercepted before reaching the read-only preview server. */
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5100';
const endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');
assert.notEqual(endpoint.port,'5002');
assert.notEqual(endpoint.port,'5112','Preserve the interactive Commands server');
const output=resolve(process.argv[2]||'/tmp/space-setup-branding');
await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE
  ?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const browser=await chromium.launch({headless:true,
  executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
const page=await context.newPage();
const report={origin,checks:[],requests:[],writes:[],errors:[],screenshots:[],layouts:[]};
const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZrS8AAAAASUVORK5CYII=','base64');
const logoFile={name:'fictional-logo.png',mimeType:'image/png',buffer:png};
const logoURL='/space/branding/logo?v=abcdef1234567890';
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
const gate=()=>({arrived:deferred(),release:deferred()});
let saved={name:'Space',logo_url:null},saveError=null,holdSave=null,holdRead=null,logoMissing=false;
const send=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),path=url.pathname,method=request.method();
  report.requests.push({path,method});
  if(url.origin!==endpoint.origin){report.errors.push('Blocked external request '+request.url());return route.abort();}
  if(path==='/space/branding'){
    if(method==='GET'){
      const snapshot=structuredClone(saved),pending=holdRead;holdRead=null;
      if(pending){pending.arrived.resolve();await pending.release.promise;}
      return send(route,snapshot);
    }
    if(method==='PUT'){
      const type=request.headers()['content-type'];
      assert.match(type,/^multipart\/form-data; boundary=/,'Browser sets the multipart boundary');
      const form=await new Request(request.url(),{method,headers:{'content-type':type},body:request.postDataBuffer()}).formData();
      const upload=form.get('logo');
      const body={name:form.get('name'),remove_logo:form.get('remove_logo'),
        logo:upload?{name:upload.name,type:upload.type,size:upload.size}:null};
      report.writes.push(body);
      assert.ok([...form.keys()].every(key=>['name','logo','remove_logo'].includes(key)),'Only branding fields are submitted');
      const pending=holdSave;holdSave=null;
      if(pending){pending.arrived.resolve();await pending.release.promise;}
      if(saveError)return send(route,{detail:saveError},503);
      saved={name:body.name.trim(),logo_url:upload?logoURL:body.remove_logo==='true'?null:saved.logo_url};
      return send(route,saved);
    }
  }
  if(path==='/space/branding/logo'&&method==='GET')return logoMissing
    ?send(route,{detail:'Logo not found'},404):route.fulfill({contentType:'image/png',body:png});
  if(method!=='GET'){
    report.errors.push('Blocked unexpected write '+method+' '+path);
    return send(route,{detail:'Fixture blocked unexpected write'},403);
  }
  return route.continue();
});
page.on('pageerror',error=>report.errors.push(error.message));
page.on('console',message=>{
  if(message.type()!=='error')return;
  if(message.location().url===origin+'/space/branding'&&message.text().includes('503'))return;
  if(logoMissing&&new URL(message.location().url||origin).pathname==='/space/branding/logo'
    &&message.text().includes('404'))return;
  report.errors.push(message.text());
});
page.on('response',response=>{
  if(response.status()<400)return;
  if(new URL(response.url()).pathname==='/space/branding'&&response.status()===503)return;
  if(logoMissing&&new URL(response.url()).pathname==='/space/branding/logo'&&response.status()===404)return;
  report.errors.push(response.status()+' '+response.url());
});
const card=page.locator('#setup-branding'),name=page.locator('#branding-name');
const file=page.locator('#branding-logo'),save=page.locator('#branding-save');
const previewName=card.locator('[data-branding-preview-name]');
const previewLogo=card.locator('[data-branding-preview-logo] img, img[data-branding-preview-logo]');
const shellName=page.locator('.brand b'),shellLogo=page.locator('.brand .mark img');
const shellDefaultMark=page.locator('.brand .mark svg[aria-label="XO"]');
const previewDefaultMark=card.locator('[data-branding-preview-logo] svg[aria-label="XO"]');
const checked=text=>{report.checks.push(text);console.log(text);};
async function waitName(expected){
  await page.waitForFunction(expected=>document.querySelector('.brand b')?.textContent===expected,expected);
}
async function waitPreview(expected){
  await page.waitForFunction(expected=>document.querySelector('[data-branding-preview-name]')?.textContent===expected,expected);
}
async function expectDefaultMark(){
  await shellDefaultMark.waitFor({state:'visible'});
  assert.equal(await shellLogo.count(),0,'The header uses the bundled logo without a custom image');
  assert.ok(await shellDefaultMark.locator('polyline').count()>0,'The default header includes its SVG artwork');
  await previewDefaultMark.waitFor({state:'visible'});
  assert.equal(await previewLogo.count(),0,'The preview also uses the bundled logo');
}
async function saveDraft(expected){
  await save.click();await waitName(expected);
  await page.waitForFunction(()=>!document.querySelector('#branding-save')?.disabled
    ||document.querySelector('#branding-status')?.textContent.toLowerCase().includes('saved'));
}
async function screenshot(label){
  await card.scrollIntoViewIfNeeded();
  await page.screenshot({path:resolve(output,label),animations:'disabled'});
  report.screenshots.push(label);
}
async function expectFileError(payload,pattern){
  const before=report.writes.length;
  await file.setInputFiles(payload);
  await page.waitForFunction(()=>!!document.querySelector('#branding-error')?.textContent.trim());
  assert.match(await page.locator('#branding-error').textContent(),pattern);
  assert.equal(report.writes.length,before,'Invalid uploads never trigger a service write');
  assert.equal(await shellName.textContent(),saved.name,'Invalid upload preserves the saved shell');
}

try{
  await page.goto(origin+'/space/#/setup/workspace',{waitUntil:'networkidle'});
  await card.waitFor();
  await page.waitForFunction(()=>document.querySelector('#branding-name')?.value==='Space');
  assert.equal(await page.locator('#setup-panel-workspace #setup-branding').count(),1);
  assert.equal(await name.getAttribute('maxlength'),'80');
  assert.equal(await name.inputValue(),'Space');
  assert.equal(await shellName.textContent(),'Space');
  assert.equal(await page.title(),'XO Space');
  await expectDefaultMark();
  assert.deepEqual(report.writes,[]);
  for(const type of ['image/png','image/jpeg','image/webp'])assert.ok((await file.getAttribute('accept')).includes(type));
  checked('Workspace owns Branding; the default name and mark load without a write.');

  const customName='Aurora <Studio>';
  await name.fill(customName);await waitPreview(customName);
  await file.setInputFiles(logoFile);
  await previewLogo.waitFor();
  assert.match(await previewLogo.getAttribute('src'),/^blob:/);
  assert.equal(await shellName.textContent(),'Space','Draft name stays in the card preview');
  assert.equal(await shellLogo.count(),0,'Draft upload stays in the card preview');
  assert.equal(await page.title(),'XO Space');
  assert.deepEqual(report.writes,[]);
  assert.match(await page.locator('#setup-step-workspace').textContent(),/Unsaved changes/);
  await page.locator('#tab-projects').click();await page.locator('#tab-setup').click();
  await card.waitFor();
  assert.equal(await name.inputValue(),customName,'Navigation retains the name draft');
  assert.match(await previewLogo.getAttribute('src'),/^blob:/,'Navigation retains the logo draft');
  await page.locator('#setup-refresh').click();
  await page.waitForFunction(()=>document.querySelector('#branding-form')?.getAttribute('aria-busy')==='false');
  assert.equal(await name.inputValue(),customName,'Refreshing status retains the name draft');
  assert.match(await previewLogo.getAttribute('src'),/^blob:/,'Refreshing status retains the logo draft');
  assert.match(await page.locator('#setup-step-workspace').textContent(),/Unsaved changes/);
  await screenshot('branding-draft-1440.png');
  const staleRead=holdRead=gate();
  await page.locator('#setup-refresh').click();await staleRead.arrived.promise;
  const pending=holdSave=gate();
  await save.click();await pending.arrived.promise;
  assert.equal(await save.isDisabled(),true,'A pending save cannot be duplicated');
  assert.equal(report.writes.length,1);
  assert.equal(await shellName.textContent(),'Space','Shell waits for save confirmation');
  pending.release.resolve();await waitName(customName);
  await page.waitForFunction(()=>document.title==='Aurora <Studio>');
  assert.deepEqual(report.writes[0].logo,{name:logoFile.name,type:'image/png',size:png.length});
  assert.equal(report.writes[0].name,customName);
  assert.ok([null,'false'].includes(report.writes[0].remove_logo));
  assert.equal(await shellName.locator('studio').count(),0,'Workspace names are rendered as text');
  assert.equal(await shellLogo.getAttribute('src'),logoURL);
  staleRead.release.resolve();
  await page.waitForFunction(()=>document.querySelector('#branding-form')?.getAttribute('aria-busy')==='false');
  assert.equal(await name.inputValue(),customName,'A late status read cannot replace the saved form');
  assert.equal(await shellName.textContent(),customName,'A late status read cannot replace the saved shell');
  assert.equal(await shellLogo.getAttribute('src'),logoURL);
  assert.doesNotMatch(await page.locator('#setup-step-workspace').textContent(),/Unsaved changes/);
  await page.reload({waitUntil:'networkidle'});await card.waitFor();await waitName(customName);
  assert.equal(await name.inputValue(),customName);
  assert.equal(await shellLogo.getAttribute('src'),logoURL);
  assert.equal(await page.title(),customName);
  checked('Name and image preview locally; multipart save updates the shell and title, persists on reload, and cannot duplicate while pending.');
  checked('Drafts and the Workspace unsaved badge survive navigation and status refresh; a read completing after save cannot roll back the form or shell.');

  const beforeMissingLogo=report.writes.length;
  logoMissing=true;
  await page.reload({waitUntil:'networkidle'});await card.waitFor();await waitName(customName);
  await expectDefaultMark();
  assert.equal(await name.inputValue(),customName,'A missing logo preserves the saved name');
  assert.equal(await page.title(),customName);
  assert.equal(report.writes.length,beforeMissingLogo,'Falling back from a missing image does not write settings');
  assert.deepEqual(saved,{name:customName,logo_url:logoURL},'Image failure does not discard saved branding');
  checked('A saved logo returning 404 restores the visible default mark in the header and preview while preserving the custom name.');
  logoMissing=false;
  await page.reload({waitUntil:'networkidle'});await card.waitFor();await waitName(customName);
  await shellLogo.waitFor({state:'visible'});
  assert.equal(await shellLogo.getAttribute('src'),logoURL);

  await page.locator('#branding-remove-logo').click();
  assert.equal(await shellLogo.count(),1,'Removing a logo is a draft until saved');
  assert.equal(report.writes.length,1);
  await save.click();
  await page.waitForFunction(()=>!document.querySelector('.brand .mark img'));
  assert.equal(report.writes.length,2);
  assert.equal(report.writes[1].remove_logo,'true');
  assert.equal(report.writes[1].logo,null);
  assert.equal(await shellName.textContent(),customName);
  await expectDefaultMark();
  await page.reload({waitUntil:'networkidle'});await card.waitFor();await waitName(customName);
  await expectDefaultMark();
  assert.equal(await name.inputValue(),customName);
  assert.equal(await page.title(),customName);
  assert.equal(report.writes.length,2,'Reloading after logo removal does not write settings');
  checked('Removing the custom logo submits the explicit removal flag and restores the visible default mark after save and reload.');

  saveError='Branding could not be saved. Please try again.';
  await name.fill('Fictional retry');await file.setInputFiles(logoFile);
  await save.click();
  await page.waitForFunction(()=>document.querySelector('#branding-error')?.textContent.includes('Please try again'));
  assert.equal(await name.inputValue(),'Fictional retry');
  assert.match(await previewLogo.getAttribute('src'),/^blob:/);
  assert.equal(await shellName.textContent(),customName);
  assert.equal(await shellLogo.count(),0);
  assert.equal(await page.title(),customName);
  saveError=null;await saveDraft('Fictional retry');
  assert.equal(report.writes.at(-1).logo.name,logoFile.name,'Retry keeps the selected file');
  checked('A failed save retains both drafts and the existing shell, and retry uses the retained upload.');

  const beforeReset=report.writes.length;
  await page.locator('#branding-reset').click();await waitPreview('Space');
  await previewDefaultMark.waitFor({state:'visible'});
  assert.equal(await name.inputValue(),'Space');
  assert.equal(await shellName.textContent(),'Fictional retry','Reset creates an explicit draft');
  assert.equal(report.writes.length,beforeReset);
  await saveDraft('Space');
  await expectDefaultMark();
  assert.equal(await page.title(),'XO Space');
  assert.equal(report.writes.at(-1).remove_logo,'true');
  await page.reload({waitUntil:'networkidle'});await card.waitFor();await waitName('Space');
  await expectDefaultMark();
  assert.equal(await name.inputValue(),'Space');
  assert.equal(await page.title(),'XO Space');
  assert.equal(report.writes.length,beforeReset+1,'Reloading after reset does not write settings');
  checked('Restore defaults previews Space and its default mark; saving and reloading retain the visible default logo, Space name and XO Space title.');

  const beforeInvalidName=report.writes.length;
  await name.fill('   ');
  if(await save.isEnabled())await save.click();
  assert.equal(report.writes.length,beforeInvalidName,'A blank name cannot be submitted');
  assert.equal(await shellName.textContent(),'Space');
  await name.fill('Valid fixture');
  await expectFileError({name:'unsupported.svg',mimeType:'image/svg+xml',buffer:Buffer.from('<svg/>')},/PNG|JPEG|WebP|image/i);
  await expectFileError({name:'too-large.png',mimeType:'image/png',buffer:Buffer.alloc(2*1024*1024+1)},/2\s*(?:Mi?B)|large|size/i);
  await file.setInputFiles(logoFile);
  await saveDraft('Valid fixture');
  checked('Blank names and unsupported or oversized uploads do not write; a valid replacement remains saveable.');

  const longName='Fictional workspace '+ 'W'.repeat(60);
  assert.equal(longName.length,80);
  await name.fill(longName);await saveDraft(longName);
  for(const width of [1440,1024,768,390,320]){
    await page.setViewportSize({width,height:1000});
    await name.fill('A fictional workspace with a longer custom name');
    await card.scrollIntoViewIfNeeded();
    const layout=await page.evaluate(()=>{
      const rect=node=>{const r=node.getBoundingClientRect();return{left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width};};
      const card=document.querySelector('#setup-branding');
      return{viewport:innerWidth,scroll:document.documentElement.scrollWidth,card:rect(card),
        brand:rect(document.querySelector('.brand')),tabs:rect(document.querySelector('.tabs')),
        resources:rect(document.querySelector('.resource-links')),
        controls:[...card.querySelectorAll('input,button')].filter(node=>node.getClientRects().length).map(node=>({id:node.id,...rect(node)}))};
    });
    report.layouts.push({width,...layout});
    assert.ok(layout.scroll<=width,'No horizontal document overflow at '+width+'px');
    assert.ok(layout.card.left>=-1&&layout.card.right<=width+1,'Branding card fits at '+width+'px');
    assert.ok(layout.brand.left>=-1&&layout.brand.right<=width+1,'Saved 80-character workspace name fits the header at '+width+'px');
    const overlap=(a,b)=>Math.min(a.right,b.right)>Math.max(a.left,b.left)+1
      &&Math.min(a.bottom,b.bottom)>Math.max(a.top,b.top)+1;
    assert.equal(overlap(layout.brand,layout.resources),false,'The saved name clears resource controls at '+width+'px');
    assert.equal(overlap(layout.brand,layout.tabs),false,'The saved name clears primary tabs at '+width+'px');
    for(const control of layout.controls)assert.ok(control.left>=-1&&control.right<=width+1,control.id+' fits at '+width+'px');
    await screenshot('branding-'+width+'.png');
  }
  checked('Branding controls and a saved 80-character name fit without header overlap at 1440px, 1024px, 768px, 390px and 320px.');
  assert.deepEqual(report.errors,[],'No unexpected browser, console or HTTP errors');
  console.log(JSON.stringify(report,null,2));
}catch(error){
  report.failure=error.stack;
  await page.screenshot({path:resolve(output,'failure.png')}).catch(()=>{});
  throw error;
}finally{
  await writeFile(resolve(output,'report.json'),JSON.stringify(report,null,2));
  await browser.close();
}
