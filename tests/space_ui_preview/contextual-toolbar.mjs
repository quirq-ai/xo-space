#!/usr/bin/env node
/* Real Space assets on the fictional fixture server. Connector/account and
   Quirq GET responses below are synthetic; all non-GET requests are blocked.
   Screenshots capture the actual app without replacing visual DOM or CSS. */
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const {chromium}=await import(process.env.PLAYWRIGHT_MODULE
  ?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5100';
const output=resolve(process.argv[2]||'/tmp/space-contextual-toolbar');
await mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true,
  executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000},
  deviceScaleFactor:1,locale:'en-US',timezoneId:'UTC',reducedMotion:'reduce'});
const page=await context.newPage();
const report={origin,fixture:'Fictional server plus synthetic Connector/account and Quirq GET responses',
  checks:[],layouts:[],screenshots:[],errors:[],writes:[]};
page.on('pageerror',error=>report.errors.push(error.message));
page.on('console',message=>{if(message.type()==='error')report.errors.push(message.text());});
page.on('response',response=>{if(response.status()>=400)report.errors.push(response.status()+' '+response.url());});
const json=(route,data)=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
const toolkits=[
  {id:'gmail',slug:'gmail',display_name:'Gmail',description:'Read messages and inbox'},
  {id:'slack',slug:'slack',display_name:'Slack',description:'Team conversation'},
  {id:'telegram',slug:'telegram',display_name:'Telegram',description:'Bot messages'},
].map((toolkit,index)=>({...toolkit,status:'ACTIVE',workspace_enabled:true,
  supports_action_prefs:true,schemes:['OAUTH2'],connected_account_id:'fictional-'+index}));
const connection=id=>({toolkit:id,configured:true,enabled:true,interval_s:900,
  collectors:['recent'],available_collectors:[{id:'recent',label:'Recent messages',default:true}],
  account_label:id==='gmail'?'dev@example.com':id==='slack'?'ops@example.com':'Demo bot',
  account_checked_at:'2026-09-14T10:00:00Z',events_total:2,last_poll_at:'2026-09-14T10:00:00Z'});
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url());
  if(url.origin!==origin||request.method()!=='GET'){
    report.writes.push(request.method()+' '+request.url());
    await json(route,{error:'External requests and writes are disabled in this fixture'});
    return;
  }
  if(/^\/api\/connectors\/(github|vercel)\/status$/.test(url.pathname))return json(route,{status:'needs_auth'});
  if(url.pathname==='/api/connectors/magicpath/status')return json(route,{cli_installed:false,skill_installed:false,logged_in:false,user:null});
  if(/^\/api\/connectors\/(gdrive|onedrive)\/remotes$/.test(url.pathname))return json(route,{remotes:[]});
  if(url.pathname==='/api/connectors/composio/toolkits')return json(route,{toolkits});
  if(url.pathname==='/api/connections')return json(route,{signed_in:true,poller_enabled:true,
    connections:toolkits.map(toolkit=>connection(toolkit.id))});
  const match=url.pathname.match(/^\/api\/connections\/(gmail|slack|telegram)$/);
  if(match)return json(route,connection(match[1]));
  if(url.pathname==='/api/quirq')return json(route,{root:{host_path:'/demo/.quirq',readable:true,writable:true},
    totals:{files:0,bytes:0},watcher:{enabled:false},activity:{},tree:[],project_outputs:{project_count:10}});
  return route.continue();
});

const search=page.locator('#view-search');
const graphSearch=page.locator('#q');
const modes={graph:'graph',dashboard:'graph',projects:'search',tree:'search',time:'search',connectors:'search',
  setup:'search',secrets:'search',wiki:'none',sharing:'none',quirq:'none'};
const placeholders={projects:'Filter projects…',tree:'Filter tree by name…',
  time:'Filter timeline projects…',connectors:'Filter connectors…',setup:'Search setup…',secrets:'Search setup…'};
async function expectMode(id){
  const mode=modes[id];
  await page.waitForFunction(({id,mode,placeholder})=>location.hash==='#/'+id
    &&document.getElementById('view-'+(id==='dashboard'?'graph':['connectors','secrets','setup'].includes(id)?'setup':id))?.classList.contains('is-active')
    &&document.querySelector('.topbar')?.dataset.toolbar===mode
    &&(mode!=='search'||(!document.getElementById('view-search').disabled
      &&document.getElementById('view-search').placeholder===placeholder))
    &&(mode!=='graph'||!document.getElementById('q').disabled),{id,mode,placeholder:placeholders[id]});
  assert.equal(await page.locator('#root-btn').isVisible(),mode==='graph',id+' root picker');
  assert.equal(await graphSearch.isVisible(),mode==='graph',id+' graph search');
  assert.equal(await search.isVisible(),mode==='search',id+' local search');
  assert.equal(await page.locator('#toolbar-controls').isHidden(),mode==='none',id+' controls');
}
async function go(id){
  if(id==='projects')await page.locator('#tab-projects').click();
  else if(['connectors','secrets','setup'].includes(id)){
    await page.locator('#tab-setup').click();
    await page.locator('#setup-nav [data-setup-go="'+(id==='setup'?'workspace':id)+'"]').click();
  }
  else if(id==='wiki')await page.locator('#wiki-link').click();
  else if(id==='quirq'){
    await page.locator('#tab-setup').click();
    await page.locator('#setup-nav [data-setup-go="server"]').click();
    await page.locator('#setup-quirq').click();
  }else{
    await page.locator('#tab-projects').click();
    await page.locator('[data-files-lens="'+id+'"]').click();
  }
  await expectMode(id);
  if(id==='time'){
    assert.equal(await page.locator('#tab-projects.is-on').count(),1,'Timeline belongs to Projects');
    assert.equal(await page.locator('[data-files-lens="time"][aria-current="true"]').count(),1);
    assert.equal(await page.locator('#tab-time').count(),0,'Timeline retains search without a primary tab');
  }
}
async function query(value,id){
  await search.fill(value);
  await search.press('ArrowDown');await search.press('Enter');
  assert.equal(new URL(page.url()).hash,'#/'+id,'Local search never selects a graph node');
}
async function screenshot(name){
  await page.mouse.move(1,999);
  await page.screenshot({path:resolve(output,name),animations:'disabled'});
  report.screenshots.push(name);
}
function checked(text){report.checks.push(text);console.log(text);}
async function rows(count){
  await page.waitForFunction(count=>document.querySelectorAll('.prj-row').length===count,count);
}
async function visibleConnectors(ids){
  await page.waitForFunction(ids=>JSON.stringify([...document.querySelectorAll('.conn-card[data-toolkit]')]
    .filter(card=>!card.hidden).map(card=>card.dataset.toolkit))===JSON.stringify(ids),ids);
}
async function layout(id,width){
  await page.waitForFunction(()=>Math.abs(document.getElementById('stage').getBoundingClientRect().y
    -document.querySelector('.topbar').getBoundingClientRect().bottom)<2);
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  const value=await page.evaluate(()=>{
    const rect=selector=>{const r=document.querySelector(selector).getBoundingClientRect();
      return{x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom};};
    const tabs=document.querySelector('.tabs');
    const buttons=[...tabs.querySelectorAll('button')];
    const first=buttons[0].getBoundingClientRect(),last=buttons.at(-1).getBoundingClientRect();
    const visibleControls=[...document.querySelectorAll('#toolbar-controls input,#root-btn,.resource-links a')]
      .filter(node=>node.getClientRects().length).map(node=>{
        const r=node.getBoundingClientRect();return{id:node.id||node.textContent.trim(),x:r.x,y:r.y,right:r.right,bottom:r.bottom};
      });
    return{viewport:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth,
      brand:rect('.brand'),tabsOverflow:tabs.scrollWidth>tabs.clientWidth+1,
      activeTab:tabs.querySelector('.is-on')?rect('.tabs .is-on'):null,
      buttonsCenter:(first.left+last.right)/2,visibleControls,
      header:rect('.topbar'),tabs:rect('.tabs'),resources:rect('.resource-links'),
      controls:rect('#toolbar-controls'),stage:rect('#stage')};
  });
  report.layouts.push({id,width,...value});
  assert.ok(value.scroll<=value.viewport,id+' document overflows at '+width+': '+value.scroll);
  assert.ok(Math.abs(value.tabs.x+value.tabs.width/2-width/2)<1,id+' primary tabs are not centered at '+width);
  if(!value.tabsOverflow)assert.ok(Math.abs(value.buttonsCenter-width/2)<1,id+' tab buttons are not centered at '+width);
  if(value.activeTab)assert.ok(value.activeTab.x>=value.tabs.x-1&&value.activeTab.right<=value.tabs.right+1,
    id+' active tab is clipped at '+width);
  if(width>1400)assert.ok(value.header.height<=58.5,id+' desktop header gained an extra row at '+width);
  const groups=[['brand',value.brand],['tabs',value.tabs],['resources',value.resources],
    ...(modes[id]==='none'?[]:[['controls',value.controls]])];
  for(const [index,[name,a]] of groups.entries())for(const [other,b] of groups.slice(index+1)){
    const overlap=Math.min(a.right,b.right)-Math.max(a.x,b.x)>1
      &&Math.min(a.bottom,b.bottom)-Math.max(a.y,b.y)>1;
    assert.equal(overlap,false,id+' '+name+' overlaps '+other+' at '+width);
  }
  for(const control of value.visibleControls)assert.ok(control.x>=-1&&control.right<=width+1,
    id+' '+control.id+' exceeds viewport at '+width);
  for(const [index,a] of value.visibleControls.entries())for(const b of value.visibleControls.slice(index+1))
    assert.equal(Math.min(a.right,b.right)-Math.max(a.x,b.x)>1
      &&Math.min(a.bottom,b.bottom)-Math.max(a.y,b.y)>1,false,
    id+' '+a.id+' overlaps '+b.id+' at '+width);
  for(const [name,bounds] of [['resources',value.resources],['tabs',value.tabs],
    ...(modes[id]==='none'?[]:[['controls',value.controls]])]){
    assert.ok(bounds.x>=-1&&bounds.right<=width+1,id+' '+name+' exceeds viewport at '+width);
    assert.ok(bounds.y>=value.header.y-1&&bounds.bottom<=value.header.bottom+1,
      id+' '+name+' exceeds header at '+width);
  }
  assert.ok(Math.abs(value.stage.y-value.header.bottom)<2,id+' stage follows header at '+width);
  return value.header.height;
}

try{
  // Boot Graph first: these views then share space.json, so persistence is
  // checked without the separate Dashboard/Graph dataset reload boundary.
  await page.goto(origin+'/space/#/graph',{waitUntil:'networkidle'});
  await expectMode('graph');
  await graphSearch.fill('Aurora');
  await page.locator('#qac.is-open').waitFor();
  const rootLabel=await page.locator('#root-btn b').textContent();
  await go('projects');await rows(10);
  assert.equal(await page.locator('#qac.is-open').count(),0);
  await go('graph');
  await page.locator('#root-btn').click();
  await page.locator('#rootdd.is-open').waitFor();
  await page.locator('#root-q').fill('Aurora');
  await page.locator('#root-ac.is-open').waitFor();
  await go('projects');
  assert.equal(await page.locator('#rootdd.is-open, #root-ac.is-open, #qac.is-open').count(),0);
  checked('Graph autocomplete and root menus close when leaving Graph.');

  await page.locator('#tab-projects').click();await page.keyboard.press('/');
  assert.equal(await search.evaluate(element=>element===document.activeElement),true);
  await query('AURORA','projects');await rows(1);
  assert.match(await page.locator('.prj-row').textContent(),/Aurora Console/);
  await search.press('/');assert.equal(await search.inputValue(),'AURORA/','Slash is text while typing');
  await search.press('Escape');await rows(10);
  assert.equal(await search.inputValue(),'');
  assert.equal(await search.evaluate(element=>element===document.activeElement),true);
  await search.press('Escape');
  assert.equal(await search.evaluate(element=>element===document.activeElement),false);
  await query('no such project','projects');await rows(0);
  await page.locator('#view-search-clear').click();await rows(10);
  assert.equal(await search.evaluate(element=>element===document.activeElement),true);
  await query('aurora','projects');await rows(1);
  checked('List search filters locally; slash, clear and two-stage Escape preserve expected focus.');

  await go('tree');assert.equal(await search.inputValue(),'');
  await query('client.test.ts','tree');
  await page.locator('.tv-leaf[data-file]').first().waitFor();
  for(const name of await page.locator('.tv-leaf[data-file] .tv-name').allTextContents())assert.equal(name,'client.test.ts');
  await screenshot('tree-search-1440.png');
  await go('projects');assert.equal(await search.inputValue(),'aurora');await rows(1);
  await go('tree');assert.equal(await search.inputValue(),'client.test.ts');
  await query('no tree matches','tree');
  await page.getByText('No names match', {exact:false}).waitFor();
  await page.locator('#view-search-clear').click();
  await page.getByText('No names match', {exact:false}).waitFor({state:'hidden'});
  await query('client.test.ts','tree');
  checked('Tree name filtering and List queries remain isolated across navigation.');

  await go('time');assert.equal(await search.inputValue(),'');
  await page.locator('[data-tmode="project"]').click();
  await query('Aurora','time');
  await page.waitForFunction(()=>document.querySelector('#tplot svg')?.textContent.includes('Aurora Console')
    &&!document.querySelector('#tplot svg')?.textContent.includes('Orbit API'));
  await page.locator('[data-tmode="file"]').click();
  assert.equal(await search.inputValue(),'Aurora');
  await query('no timeline matches','time');
  await page.getByText('No project matches the filter.',{exact:true}).waitFor();
  await query('Aurora','time');
  await screenshot('timeline-search-1440.png');
  await go('tree');assert.equal(await search.inputValue(),'client.test.ts');
  await go('time');assert.equal(await search.inputValue(),'Aurora');
  checked('Timeline filters both file and commit lanes without graph navigation; its query persists.');

  await go('connectors');await visibleConnectors(['gmail','slack','telegram']);
  assert.equal(await search.inputValue(),'');
  await query('conversation','connectors');await visibleConnectors(['slack']);
  await query('DEV@','connectors');await visibleConnectors(['gmail']);
  await page.locator('[data-toolkit="gmail"] [data-action="polling"]').click();
  const interval=page.locator('#poll-gmail [data-poll="interval"]');
  await interval.selectOption('1800');
  const form=await interval.elementHandle();
  await query('no connector matches','connectors');await visibleConnectors([]);
  await page.locator('#conn-no-match').waitFor({state:'visible'});
  await page.locator('#view-search-clear').click();await visibleConnectors(['gmail','slack','telegram']);
  assert.equal(await form.evaluate(element=>element.isConnected),true,'Search preserves the polling form');
  assert.equal(await interval.inputValue(),'1800','Unsaved polling interval survives filtering');
  await query('telegram','connectors');await visibleConnectors(['telegram']);
  await screenshot('connectors-search-1440.png');
  await go('projects');assert.equal(await search.inputValue(),'aurora');
  await go('connectors');assert.equal(await search.inputValue(),'telegram');
  assert.equal(await page.locator('#root-btn b').textContent(),rootLabel);
  checked('Connectors matches names/descriptions/accounts and preserves unsaved form identity and query.');

  for(const id of ['setup','secrets']){
    await go(id);await page.locator('.brand').click();await page.keyboard.press('/');
    assert.equal(await search.evaluate(element=>element===document.activeElement),true,id+' slash focuses Setup search');
    await search.fill('folder');
    await page.locator('.setup-search-result').filter({hasText:'Projects folder'}).click();
    assert.equal(await search.inputValue(),'');
    assert.equal(await page.locator('#xo-root-input').evaluate(element=>element===document.activeElement),true);
  }
  checked('Setup and Secrets retain working search controls and the slash shortcut.');

  for(const id of ['wiki','sharing','quirq']){
    await go(id);
    await page.locator('.brand').click();await page.keyboard.press('/');
    assert.equal(await page.evaluate(()=>['q','view-search'].includes(document.activeElement?.id)),false,
      id+' slash must not focus a hidden search');
    assert.equal(new URL(page.url()).hash,'#/'+id);
  }
  checked('Wiki, Sharing and Quirq expose no root/search controls or hidden-search shortcut.');

  for(const width of [320,390,640,1280,1440,1920]){
    await page.setViewportSize({width,height:1000});
    await go('graph');const graphHeight=await layout('graph',width);
    await screenshot('graph-'+width+'.png');
    await go('projects');const searchHeight=await layout('projects',width);
    await screenshot('list-'+width+'.png');
    for(const id of ['tree','time','connectors','setup','secrets']){await go(id);await layout(id,width);}
    for(const id of ['wiki','sharing','quirq']){
      await go(id);const height=await layout(id,width);
      assert.ok(height<=graphHeight+1&&height<=searchHeight+1,id+' header is not compact at '+width);
      if(width<=640)assert.ok(height<=searchHeight-20,id+' reserves a hidden control row at '+width);
      if(id==='wiki')await screenshot('wiki-'+width+'.png');
    }
    checked(width+'px: no overflow; graph/search/none controls fit; none-mode header is compact.');
  }
  await page.setViewportSize({width:1440,height:1000});
  await go('setup');
  const resourceSizes=await page.locator('.resource-links a').evaluateAll(nodes=>nodes.map(node=>({
    height:node.getBoundingClientRect().height,font:Number.parseFloat(getComputedStyle(node).fontSize),
    icon:node.querySelector('svg')?.getBoundingClientRect().width||0})));
  assert.ok(resourceSizes.every(size=>size.height>=38&&size.font>=13&&size.icon>=17),'Resource buttons have proportional hit areas, type and icons');
  assert.ok((await search.boundingBox()).width>=130,'Setup search remains usable beside larger resource buttons');
  checked('Wiki and GitHub buttons have proportional 38px hit areas; centered desktop tabs leave a usable Setup search.');

  await page.goto(origin+'/space/#/dashboard',{waitUntil:'networkidle'});
  await expectMode('dashboard');
  await screenshot('dashboard-1440.png');
  checked('Dashboard retains graph controls for its own dataset.');
  assert.deepEqual(report.errors,[],'Browser/console/HTTP errors');
  assert.deepEqual(report.writes,[],'No external requests or service writes');
  console.log(JSON.stringify({checks:report.checks.length,screenshots:report.screenshots.length,output}));
}catch(error){
  report.failure=error.stack;
  await screenshot('failure.png').catch(()=>{});
  throw error;
}finally{
  await writeFile(resolve(output,'report.json'),JSON.stringify(report,null,2));
  await browser.close();
}
