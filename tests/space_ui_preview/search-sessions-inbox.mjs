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
/* the Inbox over work items and sessions: the sections summary plus the rows
   of the tab and state asked for (the shapes of GET /api/inbox) */
const inboxRows=[
  {kind:'workitem',id:'release',project_id:'inbox-agents',pid:'pid-agents',title:'Release handoff',section:'agents',entity:'demo',state:'new',status:'open',
    source:{kind:'post',key:null,post:{agent:'demo',kind:'note'}},fact:{ts:'2026-09-14T10:00:00Z',kind:'note',url:null,link:null},
    claim:null,session:null,outcome:null,sessions:[],created_at:'2026-09-14T10:00:00Z',updated_at:'2026-09-14T10:00:00Z'},
  {kind:'workitem',id:'new-issue',project_id:'aurora-console',pid:'pid-aurora',title:'Improve the guide',section:'issues',entity:'fictional-workspace/aurora-console',state:'waiting',status:'open',
    source:{kind:'github',key:null,github:{repo:'fictional-workspace/aurora-console',number:7}},fact:{ts:'2026-09-14T10:00:00Z',kind:'issue.open',url:'https://github.com/fictional-workspace/aurora-console/issues/7',link:{view:'projects',project:'aurora-console'}},
    claim:null,session:{session_id:'fictional-session-7',runtime:'demo',attempt:1,exit:{status:'ok',message:null}},
    outcome:{kind:'task_proposed',summary:'A task for the guide.',draft:null,task:{title:'Rewrite the guide'},question:null,acted:[],at:'2026-09-14T10:05:00Z'},
    sessions:[],created_at:'2026-09-14T10:00:00Z',updated_at:'2026-09-14T10:05:00Z'},
];
const INBOX_STATES={open:['new','running','waiting','failed'],active:['running'],waiting:['waiting'],closed:['closed'],all:['new','running','waiting','failed','closed']};
function inboxAnswer(section,state){
  const wanted=INBOX_STATES[state]||INBOX_STATES.open;
  const count=rows=>({new:0,running:0,waiting:0,failed:0,closed:0,...Object.fromEntries(['new','running','waiting','failed','closed'].map(k=>[k,rows.filter(r=>r.state===k).length]))});
  const sections=[['connections','Connections'],['projects','Projects'],['issues','Issues'],['agents','Agents']].map(([id,label])=>{
    const mine=inboxRows.filter(r=>r.section===id);
    const entities=[...new Set(mine.map(r=>r.entity))].map(e=>({id:e,label:e,counts:count(mine.filter(r=>r.entity===e))}));
    return{id,label,counts:count(mine),entities};
  });
  const rows=inboxRows.filter(r=>(!section||r.section===section)&&wanted.includes(r.state));
  return{schema:1,generated_at:'2026-09-14T10:00:00Z',runner:{enabled:true},sections,rows,count:rows.length};
}
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
  assert.equal(request.method(),'GET','The Inbox page only reads');
  inboxReads++;
  await json(route,inboxAnswer(url.searchParams.get('section'),url.searchParams.get('state')||'open'));
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
  await page.locator('[data-slot="pagination-next"]').click();
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
  await page.locator('.inb-tabs [data-section="issues"]').click();
  await page.locator('.inb-row').first().waitFor();
  await waitSearch(true);
  assert.equal(await search.getAttribute('placeholder'),'Search loaded inbox items…');
  assert.equal(await search.inputValue(),'');
  const summary=await page.locator('.inb-sum').textContent();
  const readsBefore=inboxReads;
  await setQuery('GUIDE');
  assert.equal(await page.locator('.inb-row').count(),1,'The title is searchable');
  assert.match(await page.locator('.inb-note[role="status"]').textContent(),/1 matching of 1 loaded items/);
  assert.equal(await page.locator('.inb-sum').textContent(),summary,'The tab counts remain unchanged');
  await setQuery('nothing-like-this');
  assert.equal(await page.locator('.inb-row').count(),0);
  assert.match(await page.locator('.inb-empty').textContent(),/No loaded inbox items match/);
  await setQuery('waiting aurora-console');
  assert.equal(await page.locator('.inb-row').count(),1,'State and project terms combine');
  assert.equal(inboxReads,readsBefore,'Local search does not request a new page');
  await page.locator('[data-state="closed"]').click();
  await page.waitForFunction(()=>document.querySelector('.inb-empty')?.textContent.includes('Nothing closed yet'));
  assert.equal(await search.inputValue(),'waiting aurora-console','State pills preserve the query');
  await setQuery('');
  await page.locator('[data-state="open"]').click();
  await page.locator('.inb-row').first().waitFor();
  assert.equal(await page.locator('.inb-row').count(),1);
  await setQuery('Previous');
  await inboxPage('jobs').click();
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
  console.log('Sessions/Inbox local search: loaded counts, combined filters, pagination, scope visibility and query persistence passed; Sessions subviews fit and remain reachable at 320/390px.');
}finally{
  await browser.close();
}
