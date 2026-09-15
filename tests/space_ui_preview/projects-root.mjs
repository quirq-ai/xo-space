#!/usr/bin/env node
/* Projects root selection on real UI assets and fictional preview data.
   Browser interception blocks all external requests and service mutations. */
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5100';
const endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');
assert.ok(!['5002','5112'].includes(endpoint.port),'Use the fictional preview server');
const output=resolve(process.argv[2]||'/tmp/space-projects-root');
await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE
  ?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const report={checks:[],screenshots:[],errors:[],writes:[]};
const contexts=[];
const gate=()=>{let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve};};
async function arrived(promise){
  let timer;
  try{await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Root metadata request did not arrive')),15000);})]);}
  finally{clearTimeout(timer);}
}
async function createPage(hold=null){
  const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
  contexts.push(context);
  await context.addInitScript(()=>{
    window.projectRootFixtureDocument='same-document';window.projectCanvasBoots=0;
    const getContext=HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext=function(...args){
      if(this.id==='gcanvas')window.projectCanvasBoots++;
      return getContext.apply(this,args);
    };
  });
  await context.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(url.origin!==endpoint.origin||request.method()!=='GET'){
      report.writes.push(request.method()+' '+request.url());return route.abort();
    }
    if(hold&&url.pathname==='/xo/space.json'){
      hold.arrived.resolve();await hold.release.promise;
    }
    return route.continue();
  });
  const page=await context.newPage();page.setDefaultTimeout(15000);
  page.on('pageerror',error=>report.errors.push(error.message));
  page.on('console',message=>{if(message.type()==='error')report.errors.push(message.text());});
  page.on('response',response=>{if(response.status()>=400)report.errors.push(response.status()+' '+response.url());});
  return page;
}
const activeId=route=>route==='projects/data/list'?'projects':route==='projects/data/tree'?'tree':route==='projects/timeline'?'time':route==='projects/manage'?'project-manage':'graph';
async function active(page,route){
  await page.waitForURL('**/#/'+route);
  await page.locator('#view-'+activeId(route)+'.is-active').waitFor();
}
async function openRoot(page){
  await page.locator('#root-btn').click();await page.locator('#rootdd.is-open').waitFor();
  await page.waitForFunction(()=>!document.querySelector('#root-q').disabled);
}
function checked(text){report.checks.push(text);console.log(text);}

let page,hold;
try{
  for(const route of ['projects/data/list','projects/data/tree','projects/timeline','projects/manage']){
    page=await createPage();
    await page.goto(origin+'/space/#/'+route,{waitUntil:'networkidle'});await active(page,route);
    assert.equal(await page.locator('#root-btn').isVisible(),true);
    if(route!=='projects/timeline')assert.equal(await page.evaluate(()=>window.projectCanvasBoots),0,'Direct '+route+' does not boot the atlas');
    const root=await page.locator('#graph-root').elementHandle();
    await openRoot(page);await page.locator('#root-q').fill('Aurora Console');
    await page.locator('#root-ac button').filter({hasText:'Aurora Console'}).first().waitFor();
    if(route!=='projects/timeline')assert.equal(await page.evaluate(()=>window.projectCanvasBoots),0,'Searching roots reads metadata without booting a hidden canvas');
    assert.equal(new URL(page.url()).hash,'#/'+route,'Searching roots does not navigate');
    const entries=await page.evaluate(()=>history.length);
    await page.locator('#root-q').press('Enter');await active(page,'projects/data/graph');
    await page.waitForFunction(()=>document.querySelector('#root-name').textContent==='Aurora Console'&&/\d+/.test(document.querySelector('#fmeta')?.textContent||''));
    assert.ok(await page.evaluate(()=>window.projectCanvasBoots)>0);
    assert.equal(await page.evaluate(()=>history.length),entries+1,'Picking a root adds one Data Graph history entry');
    assert.equal(await root.evaluate(node=>node===document.querySelector('#graph-root')),true);
    assert.equal(await page.evaluate(()=>window.projectRootFixtureDocument),'same-document');
    const name=route.replaceAll('/','-')+'-root-selected.png';
    await page.screenshot({path:resolve(output,name)});report.screenshots.push(name);
    await page.goBack();await active(page,route);
    assert.equal(await root.evaluate(node=>node===document.querySelector('#graph-root')),true);
    await page.context().close();
  }
  checked('Direct Data List, Data Tree and Manage do not boot the atlas before root selection; those pages and Timeline open Data Graph once at the selected root and preserve history and control identity.');

  hold={arrived:gate(),release:gate()};
  page=await createPage(hold);
  await page.goto(origin+'/space/#/projects/data/list',{waitUntil:'domcontentloaded'});await active(page,'projects/data/list');
  await page.locator('#root-btn').click();await arrived(hold.arrived.promise);
  assert.equal(await page.locator('#root-q').isDisabled(),true);
  await page.locator('#tab-setup').click();await page.locator('#setup-panel-workspace').waitFor();
  hold.release.resolve();await page.waitForLoadState('networkidle');
  assert.equal(new URL(page.url()).hash,'#/setup/workspace');
  assert.equal(await page.locator('#root-btn').isVisible(),false);
  assert.equal(await page.locator('#rootdd.is-open').count(),0);
  assert.equal(await page.evaluate(()=>window.projectCanvasBoots),0,'A late metadata reply cannot start the atlas after leaving Projects');
  checked('Leaving Projects during a pending root read keeps the new page active and cannot reopen the picker or boot the graph.');
  assert.deepEqual(report.errors,[]);assert.deepEqual(report.writes,[]);
}catch(error){report.failure=error.stack;await page?.screenshot({path:resolve(output,'failure.png')}).catch(()=>{});throw error;}
finally{
  hold?.release.resolve();
  await writeFile(resolve(output,'report.json'),JSON.stringify(report,null,2));
  for(const context of contexts)await context.close().catch(()=>{});
  await browser.close();
}
