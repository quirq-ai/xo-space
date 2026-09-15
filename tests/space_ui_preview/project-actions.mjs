#!/usr/bin/env node
/* Shared Projects actions over actual UI assets. Fictional GET payloads are
   varied in memory; every external request and service mutation is blocked. */
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {openProjectPage,openProjectList,routeFor} from './routes.mjs';
const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5101',endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');assert.ok(!['5002','5112'].includes(endpoint.port));
const output=resolve(process.argv[2]||'/private/tmp/space-project-actions');await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
const report={checks:[],screenshots:[],layouts:[],requests:[],errors:[],writes:[]};
const revisions=new Map(),holds=new Map(),observed=new Map();
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
const hold=path=>{const gate={arrived:deferred(),release:deferred()};holds.set(path,gate);return gate;};
async function within(promise,label){let timer;try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Timed out: '+label)),10000);})]);}finally{clearTimeout(timer);}}
function mutate(path,data){
  const version=revisions.get(path)||0;
  if(path==='/api/xo-projects'&&version)data.items[0].display_name='Aurora Console refreshed '+version;
  if(['/xo/space.json','/xo/dashboard.json'].includes(path)&&version){
    const label='refresh-sentinel-'+version+'.md';
    data.leaves.push({...data.leaves[0],id:'fixture-refresh-'+version,label,path:'aurora-console/'+label});
    if(data.gitHistory){const key=Object.keys(data.gitHistory)[0];data.gitHistory[key][0].n+=version;}
  }
  if(path==='/api/project-sharing/status'&&version)data.watch_branch='fixture-refresh-'+version;
  observed.set(path,structuredClone(data));return data;
}
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),path=url.pathname;
  if(url.origin!==endpoint.origin||request.method()!=='GET'){report.writes.push(request.method()+' '+request.url());return route.abort();}
  report.requests.push(path);
  const pending=holds.get(path);if(pending){holds.delete(path);pending.arrived.resolve();await pending.release.promise;}
  if(['/api/xo-projects','/xo/space.json','/xo/dashboard.json','/api/project-sharing/status'].includes(path)){
    try {
      const response=await route.fetch();return await route.fulfill({response,json:mutate(path,await response.json())});
    } catch (error) {
      // Closing a page cancels its pending fixture reads. Active-page failures
      // remain test failures rather than escaping the route handler unobserved.
      if (request.frame().page().isClosed()) return;
      report.errors.push(path+': '+error.message);
      return route.abort().catch(()=>{});
    }
  }
  return route.continue();
});
async function newPage(route){
  const page=await context.newPage();page.setDefaultTimeout(15000);
  page.on('pageerror',error=>report.errors.push(error.message));
  page.on('console',message=>{if(message.type()==='error')report.errors.push(message.text());});
  await page.addInitScript(()=>{
    window.projectActionsSentinel='same-document';window.projectHandoffs=[];
    addEventListener('space:add-project',()=>window.projectHandoffs.push('add'));
  });
  await page.goto(origin+'/space/'+route,{waitUntil:'networkidle'});return page;
}
const page=await newPage(routeFor('projects'));
// Add belongs to Manage; Refresh is shared section chrome.
const add=()=>page.locator('#manage-project-add');
const refresh=()=>page.locator(new URL(page.url()).hash==='#/inbox/sharing'?'#section-refresh':'#project-refresh');
const manage=()=>page.locator('#section-nav').getByRole('link',{name:'Manage',exact:true});
const checked=text=>{report.checks.push(text);console.log(text);};
async function shot(name){await page.mouse.move(1,999);await page.screenshot({path:resolve(output,name),animations:'disabled'});report.screenshots.push(name);}
async function reread(id,path,verify){
  await openProjectPage(page,id);await page.waitForLoadState('networkidle');
  const before=report.requests.filter(value=>value===path).length;
  revisions.set(path,(revisions.get(path)||0)+1);
  const pending=hold(path);await refresh().click();await within(pending.arrived.promise,id+' refresh read');
  assert.equal(await refresh().isDisabled(),true,id+' prevents duplicate refreshes');
  await refresh().evaluate(node=>node.click());
  assert.equal(report.requests.filter(value=>value===path).length,before+1,id+' starts exactly one forced read');
  pending.release.resolve();await page.waitForFunction(()=>[...document.querySelectorAll('#project-refresh,#section-refresh')].some(node=>node.getClientRects().length&&!node.disabled));
  await verify();assert.equal(new URL(page.url()).hash,routeFor(id));
  assert.equal(await page.evaluate(()=>window.projectActionsSentinel),'same-document');
}
async function layout(id,width){
  const geometry=await page.locator('#section-nav').evaluate(nav=>{
    const rect=node=>{const r=node.getBoundingClientRect();return{left:r.left,right:r.right,top:r.top,bottom:r.bottom};};
    const nodes=[document.querySelector('#root-btn'),document.querySelector('#project-refresh')];
    return{nav:rect(nav),nodes:nodes.map(rect),scroll:document.documentElement.scrollWidth,width:innerWidth};
  });
  assert.equal(await page.locator('#section-nav #project-add,#section-nav .section-nav-action[href="#/projects/manage"]').count(),0,'Manage is a page, with no global Add or Manage action');
  assert.equal(geometry.nodes.length,2);assert.ok(geometry.scroll<=width);
  for(const node of geometry.nodes)assert.ok(node.left>=-1&&node.right<=width+1&&node.bottom<=geometry.nav.bottom+1,id+' actions fit at '+width);
  for(let a=0;a<geometry.nodes.length;a++)for(let b=a+1;b<geometry.nodes.length;b++){
    const x=geometry.nodes[a],y=geometry.nodes[b];assert.ok(Math.min(x.right,y.right)-Math.max(x.left,y.left)<=1||Math.min(x.bottom,y.bottom)-Math.max(x.top,y.top)<=1,id+' actions do not overlap');
  }
  if(width===1440)assert.ok(geometry.nodes.every(node=>Math.abs(node.top-geometry.nodes[0].top)<3),id+' actions align on desktop');
  report.layouts.push({id,width,...geometry});
}
try{
  await openProjectList(page);await page.locator('#prj-row-aurora-console').waitFor();
  assert.equal(await page.locator('#view-projects #prj-add,#view-projects #prj-refresh').count(),0);
  await openProjectPage(page,'manage');await page.locator('[data-project-pin="aurora-console"]').click();
  await openProjectList(page);await page.locator('#view-search').fill('Aurora');
  await page.locator('#prj-filter').selectOption('pinned');await page.locator('#prj-row-aurora-console .prj-row-head').click();
  await page.locator('.prj-drawer:not([hidden]) [data-panel="files"] [data-cd="src"]').click();await page.locator('.prj-drawer:not([hidden]) [data-panel="files"] [data-file="src/main.ts"]').waitFor();
  const drawer=await page.locator('#prj-drawer-aurora-console').elementHandle();
  await reread('projects','/api/xo-projects',async()=>{
    await page.getByText('Aurora Console refreshed 1',{exact:true}).waitFor();
    assert.equal(await page.locator('#view-search').inputValue(),'Aurora');assert.equal(await page.locator('#prj-filter').inputValue(),'pinned');
    assert.equal(await drawer.evaluate(node=>node===document.querySelector('#prj-drawer-aurora-console')),true);
    await page.locator('.prj-drawer:not([hidden]) [data-panel="files"] [data-file="src/main.ts"]').waitFor();
  });
  checked('Shared Refresh rereads the List catalog while preserving query, pins, drawer and current folder.');
  for(const [id,path] of [['dashboard','/xo/dashboard.json'],['graph','/xo/space.json']]){
    await openProjectPage(page,id);await page.locator('#root-btn').click();await page.locator('#root-q').fill('Aurora Console');
    await page.locator('#root-ac button').filter({hasText:'Aurora Console'}).first().click();
    const rootName=await page.locator('#root-name').textContent();
    await reread(id,path,async()=>{
      await page.waitForFunction(count=>document.querySelector('#counts')?.textContent.includes(String(count)),observed.get(path).leaves.length);
      assert.equal(await page.locator('#root-name').textContent(),rootName,id+' retains the selected root');
      assert.equal(await page.locator('#cmdk-trigger').isVisible(),true,id+' keeps the command palette trigger');
    });
  }
  checked('Overview and Graph Refresh replace their active datasets without a document reload and retain the chosen root.');
  await openProjectPage(page,'tree');await page.waitForFunction(()=>document.querySelector('#view-search').placeholder==='Filter tree by name…');await page.locator('#view-search').fill('refresh-sentinel-2');
  await reread('tree','/xo/space.json',async()=>{
    await page.getByText('refresh-sentinel-2.md',{exact:true}).waitFor();
    assert.equal(await page.locator('#view-search').inputValue(),'refresh-sentinel-2');
    assert.equal(await page.locator('[data-tv="reload"]').count(),0);
  });
  await openProjectPage(page,'time');await page.waitForFunction(()=>document.querySelector('#view-search').placeholder==='Filter timeline projects…'&&!document.querySelector('#view-search').disabled);await page.locator('#view-search').fill('Aurora');
  await page.locator('[data-tmode="project"]').click();
  assert.equal(await page.locator('#view-search').inputValue(),'Aurora','Timeline query is entered after its toolbar activates');
  await reread('time','/xo/space.json',async()=>{
    const snapshot=observed.get('/xo/space.json'),key=Object.keys(snapshot.gitHistory)[0];
    const total=snapshot.gitHistory[key].reduce((sum,day)=>sum+day.n,0);
    await page.locator('#tplot').getByText(total+' COMMITS',{exact:true}).waitFor();
    assert.equal(await page.locator('#view-search').inputValue(),'Aurora');
    assert.equal(await page.locator('[data-tmode="project"]').getAttribute('class'),'is-on');
  });
  await openProjectPage(page,'sharing');await page.locator('#section-nav [data-act="composer"]').click();
  await page.locator('#shl-composer .shl-pick[data-id="aurora-console"]').click();
  await page.locator('#shl-composer input[name="ws"]').fill('fixture-recipient-draft');
  const recipient=await page.locator('#shl-composer input[name="ws"]').elementHandle();
  await reread('sharing','/api/project-sharing/status',async()=>{
    await page.getByText(/watching fixture-refresh-1/).waitFor();
    assert.equal(await recipient.evaluate(node=>node===document.querySelector('#shl-composer input[name=ws]')),true);
    assert.equal(await page.locator('#shl-composer input[name="ws"]').inputValue(),'fixture-recipient-draft');
  });
  await page.locator('#section-nav [data-act="composer"]').click();
  checked('Tree, Timeline and Sharing Refresh display new source data; Tree search and Timeline mode/query persist.');

  await openProjectList(page);await manage().click();await page.waitForURL('**/#/projects/manage');await page.locator('#manage-project-add').waitFor();
  assert.equal(await page.locator('#manage-project-form').isVisible(),false,'Manage opens the project manager');
  await add().click();await page.locator('#manage-project-repository').waitFor();
  await page.waitForFunction(()=>document.activeElement===document.querySelector('#manage-project-repository'),undefined,{timeout:3000}).catch(async error=>{
    report.addFocus=await page.locator('#manage-project-repository').evaluate(node=>({active:{tag:document.activeElement.tagName,id:document.activeElement.id},visible:!!node.getClientRects().length,disabled:node.disabled,events:window.projectHandoffs,route:location.hash}));throw error;
  });
  await page.locator('#manage-project-repository').fill('https://github.com/fictional/keep-draft.git');
  const draft=await page.locator('#manage-project-repository').elementHandle();
  await openProjectPage(page,'tree');await manage().click();await add().click();
  assert.equal(await draft.evaluate(node=>node===document.querySelector('#manage-project-repository')),true);
  assert.equal(await page.locator('#manage-project-repository').inputValue(),'https://github.com/fictional/keep-draft.git');
  await reread('manage','/api/xo-projects',async()=>{
    await page.getByText('Aurora Console refreshed 2',{exact:true}).waitFor();
    assert.equal(await page.locator('#manage-project-repository').inputValue(),'https://github.com/fictional/keep-draft.git');
    assert.equal(await draft.evaluate(node=>node===document.querySelector('#manage-project-repository')),true);
  });
  checked('Manage owns Add; opening it leaves the form closed, while Add focuses the clone form and refresh retains its draft.');
  for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:1000});
    for(const id of ['dashboard','projects','graph','tree','time','manage']){
      await openProjectPage(page,id);await page.waitForLoadState('networkidle');await layout(id,width);
      await shot(id+'-'+width+'.png');
    }
  }
  checked('All six Projects pages keep Graph root and Refresh together, with Manage as a page at1440,390 and320px.');
  assert.deepEqual(report.errors,[]);assert.deepEqual(report.writes,[]);
}catch(error){report.failure=error.stack;await shot('failure.png').catch(()=>{});throw error;}
finally{for(const pending of holds.values())pending.release.resolve();await writeFile(resolve(output,'report.json'),JSON.stringify(report,null,2));await browser.close();}
