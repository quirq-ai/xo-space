/* Run with the local fixture server from this directory. API responses below
   are synthetic and writes stay in memory; no real Inbox or sessions are read. */
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';

const {chromium}=await import(process.env.PLAYWRIGHT_MODULE
  ?pathToFileURL(process.env.PLAYWRIGHT_MODULE).href:'playwright');
const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5100';
const browser=await chromium.launch({headless:true,
  executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000}});
const page=await context.newPage();
const errors=[];
page.on('pageerror',error=>errors.push(error.message));
const sessions=Array.from({length:24},(_,i)=>({
  id:'session-'+String(i+1).padStart(2,'0'),
  agent:i%3?'demo_beta':'demo_alpha',
  project:i<12?'Aurora Console':'Orbit API',
  project_path:i<12?'/demo/aurora-console':'/demo/orbit-api',
  model:i%2?'model-comet':'model-atlas',
  started_at:new Date(Date.UTC(2026,8,14,0,i)).toISOString(),
  ended_at:new Date(Date.UTC(2026,8,14,0,i+1)).toISOString(),
  duration_sec:60,total_tokens:100+i,cost:0,cost_known:false,
  turns:2,subagents:[],tools:[],
}));
const inbox=[
  {id:'issue-a',status:'new',source:'issues',kind:'issue',title:'Release coordination',
    body:'Prepare the lantern handoff.',project_id:'aurora-console'},
  {id:'connection-b',status:'new',source:'connections',kind:'calendar',title:'Review meeting',
    body:'Discuss the rollout schedule.',project_id:'harbor-infra'},
  {id:'todo-c',status:'seen',source:'todos',kind:'todo',title:'Update the runbook',
    body:'Document the recovery steps.',project_id:'harbor-infra'},
  {id:'agent-d',status:'done',source:'demo-agent',kind:'note',title:'Previous release',
    body:'The earlier handoff is finished.',project_id:'aurora-console'},
].map(item=>({...item,ts:'2026-09-14T10:00:00Z'}));
const writes=[];
let inboxReads=0;
const json=(route,data)=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
await context.route('**/xo/sessions.json',route=>json(route,{
  meta:{sources:[
    {id:'demo_alpha',label:'Runtime Alpha',available:true},
    {id:'demo_beta',label:'Runtime Beta',available:true},
  ]},
  totals:{sessions:120,sessions_by_agent:{demo_alpha:80,demo_beta:40}},
  sessions,daily_models:[],daily_sessions:[],daily_tools:[],
}));
await context.route('**/data/session_prompts.json?*',route=>json(route,{
  supported:false,prompts:[],total_prompts:0,
}));
await context.route('**/api/connections',route=>json(route,{connections:[]}));
await context.route('**/api/inbox**',async route=>{
  const request=route.request();
  const url=new URL(request.url());
  if(request.method()==='PATCH'){
    const payload=request.postDataJSON();
    writes.push(payload);
    for(const item of inbox)if(payload.ids.includes(item.id))item.status=payload.status;
    await json(route,{changed:payload.ids.length,missing:[]});
    return;
  }
  assert.equal(request.method(),'GET','Only the explicit mock bulk action may write');
  inboxReads++;
  const status=url.searchParams.get('status')||'open';
  const filtered=inbox.filter(item=>status==='all'||(status==='open'?item.status!=='done':item.status===status));
  const counts={new:0,seen:0,done:0};
  for(const item of inbox)counts[item.status]++;
  counts.open=counts.new+counts.seen;
  await json(route,{items:filtered.slice(0,Number(url.searchParams.get('limit'))||200),counts,total:filtered.length});
});

const search=page.locator('#view-search');
const rows=page.locator('#sess-body tr[data-sid]');
const caption=page.locator('#sess-body [role="status"]');
async function waitSearch(visible){
  await search.waitFor({state:visible?'visible':'hidden'});
}
async function setQuery(value){
  await search.fill(value);
}
async function expectRows(count){
  await page.waitForFunction(count=>document.querySelectorAll('#sess-body tr[data-sid]').length===count,count);
}
const agentPage=sub=>page.locator('#section-nav [href="#/agents/'+sub+'"]');
const inboxPage=sub=>page.locator('#section-nav [href="#/inbox/'+sub+'"]');

try{
  await page.goto(origin+'/space/#/agents',{waitUntil:'networkidle'});
  await agentPage('overview').waitFor();
  await waitSearch(false);
  await agentPage('sessions').click();
  await waitSearch(true);
  assert.equal(await search.getAttribute('placeholder'),'Search loaded sessions…');
  await expectRows(10);
  assert.match(await caption.textContent(),/newest 24 loaded of 120 selected sessions/);
  await page.locator('#sess-next').click();
  assert.match(await page.locator('.sess-pager').textContent(),/Page 2 of 3/);
  await setQuery('AuRoRa model-comet');
  await expectRows(6);
  assert.match(await caption.textContent(),/6 matching of 24 loaded sessions/);
  assert.match(await caption.textContent(),/120 selected sessions/);
  for(const row of await rows.all())assert.match(await row.textContent(),/Aurora Console[\s\S]*model-comet/);
  await page.locator('[data-k="project"]').click();
  await expectRows(6);
  await setQuery('Runtime Alpha');
  await expectRows(8);
  await page.locator('[data-agent="demo_alpha"]').uncheck();
  await expectRows(0);
  assert.match(await caption.textContent(),/0 matching of 16 loaded sessions/);
  assert.match(await page.locator('#sess-body').textContent(),/No loaded sessions match/);
  await page.locator('[data-agent="demo_alpha"]').check();
  await expectRows(8);
  await setQuery('session-04');
  await expectRows(1);
  await rows.first().click();
  await page.locator('#sess-back').waitFor();
  await waitSearch(false);
  await page.locator('#sess-back').click();
  await waitSearch(true);
  assert.equal(await search.inputValue(),'session-04');
  for(const sub of ['overview','tools','models','trends']){
    await agentPage(sub).click();
    await waitSearch(false);
  }
  await agentPage('sessions').click();
  await waitSearch(true);
  assert.equal(await search.inputValue(),'session-04');
  await setQuery('');
  await expectRows(10);
  assert.match(await page.locator('.sess-pager').textContent(),/Page 1 of 3/);

  await page.locator('#tab-inbox').click();
  await page.locator('.inb-row').first().waitFor();
  await waitSearch(true);
  assert.equal(await search.getAttribute('placeholder'),'Search loaded inbox items…');
  assert.equal(await search.inputValue(),'');
  const summary=await page.locator('.inb-sum').textContent();
  const readsBefore=inboxReads;
  await setQuery('LANTERN');
  assert.equal(await page.locator('.inb-row').count(),1,'The body is searchable before expansion');
  assert.match(await page.locator('.inb-note[role="status"]').textContent(),/1 matching of 3 loaded items/);
  assert.equal(await page.locator('.inb-sum').textContent(),summary,'Global counts remain unchanged');
  await page.locator('[data-src="workspace"]').click();
  assert.equal(await page.locator('.inb-row').count(),0);
  assert.match(await page.locator('.inb-empty').textContent(),/No loaded inbox items match/);
  await page.locator('[data-src="issues"]').click();
  assert.equal(await page.locator('.inb-row').count(),1);
  await setQuery('issues aurora-console');
  assert.equal(await page.locator('.inb-row').count(),1,'Source and project terms combine');
  assert.equal(inboxReads,readsBefore,'Local search/source filtering does not request a new page');
  const bulk=page.locator('[data-act="mark-all"]');
  assert.equal(await bulk.textContent(),'Mark all loaded seen');
  assert.match(await page.locator('.inb-note[role="status"]').textContent(),/includes items hidden/);
  const patched=page.waitForResponse(response=>response.request().method()==='PATCH');
  await bulk.click();
  await patched;
  assert.deepEqual(writes,[{ids:['issue-a','connection-b'],status:'seen'}],
    'Bulk action keeps the explicitly disclosed full loaded-page scope');
  await page.locator('[data-filter="done"]').click();
  await page.waitForFunction(()=>document.querySelector('.inb-note[role="status"]')?.textContent.includes('0 matching of 1 loaded items'));
  assert.equal(await search.inputValue(),'issues aurora-console','Status filters preserve the query');
  await setQuery('');
  await page.locator('[data-src="all"]').click();
  assert.equal(await page.locator('.inb-row').count(),1);
  await setQuery('Previous');
  await inboxPage('connections').click();
  await waitSearch(false);
  await inboxPage('items').click();
  await waitSearch(true);
  assert.equal(await search.inputValue(),'Previous','Inbox query survives its own page changes');
  await page.locator('#tab-agents').click();
  await waitSearch(false);
  await agentPage('sessions').click();
  await waitSearch(true);
  assert.equal(await search.inputValue(),'','Agents retains its own cleared query');
  await page.locator('#tab-inbox').click();
  assert.equal(await search.inputValue(),'Previous','Inbox retains its query across views');
  for(const width of [320,390]){
    await page.setViewportSize({width,height:1000});
    await page.goto(origin+'/space/#/agents',{waitUntil:'networkidle'});
    await agentPage('overview').waitFor();
    for(const sub of ['overview','sessions','tools','models','trends']){
      await agentPage(sub).click();
      await page.waitForFunction(sub=>document.querySelector('#section-nav [href="#/agents/'+sub+'"]')?.getAttribute('aria-current')==='page',sub);
      const bounds=await agentPage(sub).evaluate(link=>{
        const nav=link.closest('.section-nav-links').getBoundingClientRect();
        const box=link.getBoundingClientRect();
        return nav.left>=0&&nav.right<=innerWidth+1&&box.left>=nav.left-1&&box.right<=nav.right+1;
      });
      assert.equal(bounds,true,width+'px active Agents page remains visible within its navigation');
    }
    await agentPage('sessions').click();
    await waitSearch(true);
    await setQuery('Aurora');
    await page.waitForTimeout(750);
    await page.screenshot({path:'/tmp/space-search-sessions-'+width+'.png'});
  }
  assert.deepEqual(errors,[]);
  console.log('Sessions/Inbox local search: loaded counts, combined filters, pagination, scope visibility, query persistence and bulk scope passed; Sessions subviews fit and remain reachable at 320/390px.');
}finally{
  await browser.close();
}
