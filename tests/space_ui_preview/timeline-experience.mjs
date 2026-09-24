#!/usr/bin/env node
/* Timeline over an explicit fictional graph: all service writes are blocked. */
import assert from 'node:assert/strict';
import {startDataRefresh,waitForDataRefresh} from './refresh-helpers.mjs';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {openProjectList,openProjectPage} from './routes.mjs';
const origin=process.env.SPACE_PREVIEW_URL||'http://127.0.0.1:5101',endpoint=new URL(origin);
assert.equal(endpoint.hostname,'127.0.0.1');assert.ok(!['5002','5112'].includes(endpoint.port));
const output=resolve(process.argv[2]||'/private/tmp/space-timeline-experience');await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href:'playwright');
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1000},locale:'en-US',timezoneId:'UTC',reducedMotion:'reduce'});
const page=await context.newPage();page.setDefaultTimeout(15000);
const report={checks:[],screenshots:[],layouts:[],errors:[],writes:[]};
const projects=[['alpha','Aurora Console','#a8d94f'],['beta','Orbit API','#72b7db'],['gamma','Field Notes','#e5b772']];
const dates={alpha:['2024-02-10','2025-06-15','2026-08-20'],beta:['2025-06-15','2026-07-01',null],gamma:[null]};
const histories={p_alpha:[{d:'2024-02-10',n:2},{d:'2025-06-15',n:3},{d:'2026-08-20',n:4}],p_beta:[{d:'2025-06-15',n:1},{d:'2026-07-01',n:5}]};
function graph({empty=false}={}){return{
  meta:{title:'Fictional workspace',tagline:'Timeline review',mappedOn:'15 September 2026',hubLabel:'Project',rootEdgeLabel:'a project in this workspace'},
  categories:Object.fromEntries(projects.map(([id,name,color])=>['p_'+id,{name,color}])),
  root:{id:'xo',label:'Review workspace',blurb:'Fictional timeline validation'},
  hubs:projects.map(([id,label])=>({id:'p_'+id,cat:'p_'+id,label,blurb:'Fictional project'})),
  groups:projects.map(([id])=>({id:'g_'+id,cat:'p_'+id,label:'docs',blurb:'Project documentation'})),
  leaves:projects.flatMap(([id])=>dates[id].map((date,index)=>({id:'f_'+id+'_'+index,group:'g_'+id,shape:'disc',tag:'Document',
    label:'guide-'+index+'.md',date:empty?null:date,path:id+'/docs/guide-'+index+'.md',blurb:'Fictional versioned document'}))),
  ties:[],hubAngles:Object.fromEntries(projects.map(([id],index)=>['p_'+id,index*Math.PI*2/3])),
  timeline:{start:'2024-02-01',end:'2026-09-01'},milestones:[{d:'2025-06-15',t:'Documentation milestone'},
    ...(longMilestones?[{d:'2026-07-01',t:'Documentation milestone with a longer release note that wraps on small screens'}]:[])],
  gitHistory:empty?{}:structuredClone(histories),
};}
let empty=false,longMilestones=false;
const json=(route,data)=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url());
  if(url.origin!==endpoint.origin||request.method()!=='GET'){report.writes.push(request.method()+' '+request.url());return route.abort();}
  if(['/xo/space.json','/xo/dashboard.json'].includes(url.pathname))return json(route,graph({empty}));
  if(url.pathname==='/api/xo-projects')return json(route,{items:projects.map(([id,display_name])=>({id,display_name,description:'Fictional project',created_at:'2024-02-10T00:00:00Z',unscaffolded:false}))});
  if(url.pathname.endsWith('/todos'))return json(route,{sessions:{}});
  return route.continue();
});
page.on('pageerror',error=>report.errors.push(error.message));
page.on('console',message=>{if(message.type()==='error')report.errors.push(message.text());});
page.on('response',response=>{if(response.status()>=400)report.errors.push(response.status()+' '+response.url());});
const checked=text=>{report.checks.push(text);console.log(text);};
const search=page.locator('#view-search');
const mode=value=>page.locator('[data-tmode="'+value+'"]');
async function ready(){await page.locator('#view-time.is-active').waitFor();await page.locator('#tplot svg').waitFor();
  await page.waitForFunction(()=>document.querySelector('#view-search')?.placeholder==='Filter timeline projects…'&&!document.querySelector('#view-search').disabled);}
async function settle(){await page.evaluate(()=>new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done))));}
async function screenshot(name){await page.mouse.move(1,999);await page.screenshot({path:resolve(output,name),animations:'disabled'});report.screenshots.push(name);}
const summary=()=>page.locator('#tsummary');
async function expectSummary(...parts){
  await page.waitForFunction(parts=>{const text=document.querySelector('#tsummary')?.textContent||'';return parts.every(part=>text.includes(part));},parts);
}
async function year(value){await page.locator('[data-year="'+value+'"]').click();await settle();}
async function layout(label,width){
  const bounds=await page.evaluate(()=>{
    const rect=node=>{const r=node.getBoundingClientRect();return{left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height};};
    return{viewport:innerWidth,scroll:document.documentElement.scrollWidth,content:rect(document.querySelector('.timeline-content')),
      plot:rect(document.querySelector('#tplot')),nav:rect(document.querySelector('#section-nav')),
      heading:[...document.querySelectorAll('#view-time h1,#view-time h2')].map(rect),
      controls:[...document.querySelectorAll('#view-time button,#view-time input')].filter(node=>!node.hidden&&node.getClientRects().length).map(rect)};
  });
  assert.ok(bounds.scroll<=width,label+' has no document overflow');
  assert.ok(bounds.content.width<=1181,label+' follows Data content width');
  assert.ok(Math.abs(bounds.content.left-(width-bounds.content.width)/2)<2,label+' is centered');
  assert.ok(bounds.plot.height>=350,label+' retains a substantial plot');
  assert.ok(bounds.plot.top>=bounds.nav.bottom,label+' clears section navigation');
  assert.ok(bounds.heading.length===1&&bounds.heading[0].height<=2,label+' has an accessible heading without repeated visible title');
  assert.ok(bounds.controls.every(r=>r.left>=-1&&r.right<=width+1),label+' controls fit the viewport');
  report.layouts.push({label,...bounds});
}
try{
  await page.goto(origin+'/space/#/projects/timeline',{waitUntil:'networkidle'});await ready();
  await expectSummary('5 dated files','3 mapped projects');
  assert.equal(await page.locator('#tdots [data-id]').count(),5,'Only Git-dated files render');
  const initialSummary=await summary().textContent();
  assert.match(initialSummary,/2024/);assert.match(initialSummary,/2026/);
  await mode('project').focus();await mode('project').press('Space');await settle();
  assert.equal(await mode('project').getAttribute('aria-pressed'),'true');
  assert.equal(await mode('file').getAttribute('aria-pressed'),'false');
  assert.equal(await mode('project').evaluate(node=>node===document.activeElement),true);
  assert.equal(await page.locator('#tplot [data-hist]').count(),5);
  await expectSummary('15 commits','3 mapped projects');
  await mode('file').click();await expectSummary('5 dated files');
  checked('The summary distinguishes dated files from commit totals; keyboard mode controls retain focus and announce the active choice.');

  await year('2025');await expectSummary('2 dated files','3 mapped projects');
  const yearSummary=await summary().textContent();assert.notEqual(yearSummary,initialSummary);assert.match(yearSummary,/2025/);
  await mode('project').click();await expectSummary('4 commits');
  await search.fill('Aurora');await expectSummary('3 commits','1 of 3 mapped projects');
  await mode('file').click();await expectSummary('1 dated file','1 of 3 mapped projects');
  assert.equal(await search.inputValue(),'Aurora');
  const rangeSummary=await summary().textContent(),scrub=page.locator('#tscrub');
  await scrub.focus();await scrub.press('Home');await settle();
  assert.equal(await scrub.inputValue(),'0');assert.equal(await summary().textContent(),rangeSummary,'Scrubbing changes playback position, not window totals');
  const startReadout=await page.locator('#treadout').textContent();await scrub.press('End');await settle();
  assert.equal(await scrub.inputValue(),'1000');assert.notEqual(await page.locator('#treadout').textContent(),startReadout);
  await page.locator('#tplay').click();await page.waitForFunction(()=>document.querySelector('#tplay span').textContent==='Pause');
  await page.waitForFunction(()=>Number(document.querySelector('#tscrub').value)>0&&Number(document.querySelector('#tscrub').value)<1000);
  await page.locator('#tplay').click();assert.equal(await page.locator('#tplay span').textContent(),'Play');
  assert.equal(await summary().textContent(),rangeSummary,'Playback does not recalculate totals against the moving cursor');
  await search.fill('');await year('all');await expectSummary('5 dated files','3 mapped projects');
  const plotBox=await page.locator('#tplot').boundingBox();await page.mouse.move(plotBox.x+plotBox.width/2,plotBox.y+plotBox.height/2);
  await page.mouse.wheel(0,-450);await page.waitForFunction(old=>document.querySelector('#tsummary').textContent!==old,initialSummary);
  assert.equal(await page.locator('[data-year="all"]').getAttribute('aria-pressed'),'false');
  await year('all');await expectSummary('5 dated files');
  checked('Year and project filters update current-window totals; zoom/reset, keyboard scrub and Play retain their existing behavior.');

  await search.fill('Field Notes');await expectSummary('0 dated files','1 of 3 mapped projects');
  assert.match(await page.locator('#tsub').textContent(),/no|without/i);
  await page.locator('#tplot').getByText(/NO DATED FILES/).waitFor();
  await search.fill('missing-project');await expectSummary('No matching projects');
  assert.match(await page.locator('#tsub').textContent(),/Try a different project name/i);
  await page.locator('#tplot').getByText('No project matches the filter.',{exact:true}).waitFor();
  await screenshot('timeline-no-matches-1440.png');
  await search.fill('');await expectSummary('5 dated files');
  checked('Projects without history remain distinct from a project search with no matches.');

  await page.locator('#tdots [data-id="f_alpha_1"]').click();await page.waitForURL('**/#/projects/data/graph');
  await page.locator('#panel.is-open .conn[data-id="g_alpha"]').click();
  await page.locator('#panel [data-act="timeline"]').click();await page.waitForURL('**/#/projects/timeline');await ready();
  await page.locator('#tclear').waitFor({state:'visible'});await expectSummary('5 dated files');
  const trace=await page.locator('#tsub').textContent();assert.match(trace,/docs/);
  await year('2025');assert.equal(await page.locator('#tsub').textContent(),trace,'Window changes retain the trace context');
  await page.locator('#tclear').click();await page.locator('#tclear').waitFor({state:'hidden'});
  assert.notEqual(await page.locator('#tsub').textContent(),trace);await year('all');
  checked('Graph-to-Timeline traces keep their context separate from summary counts and can be cleared.');

  await search.fill('Aurora');await mode('project').click();await expectSummary('9 commits');
  await openProjectPage(page,'dashboard');await openProjectPage(page,'time');await ready();
  assert.equal(await search.inputValue(),'Aurora');assert.equal(await mode('project').getAttribute('aria-pressed'),'true');await expectSummary('9 commits');
  await page.reload({waitUntil:'networkidle'});await ready();assert.equal(await mode('project').getAttribute('aria-pressed'),'true');
  await search.fill('');await expectSummary('15 commits');
  checked('Timeline project search survives changing projections and the chosen mode survives reload.');

  for(const value of ['file','project']){
    await mode(value).click();await expectSummary(value==='file'?'5 dated files':'15 commits');
    for(const width of [1440,390,320]){
      await page.setViewportSize({width,height:1000});await settle();await layout(value+' '+width,width);
      await screenshot('timeline-'+value+'-'+width+'.png');
    }
  }
  checked('Timeline summary, rectangular controls and plot fit 1440px, 390px and 320px without repeating the title.');

  longMilestones=true;await startDataRefresh(page);await waitForDataRefresh(page);
  await expectSummary('15 commits');
  const captionBounds=[];
  for(const [position,text] of [[600,'Documentation milestone'],[950,'Documentation milestone with a longer release note that wraps on small screens']]){
    await scrub.evaluate((node,value)=>{node.value=String(value);node.dispatchEvent(new Event('input',{bubbles:true}));},position);
    await page.waitForFunction(text=>document.querySelector('#tmilestone').textContent==='◆ milestone · '+text,text);
    await page.waitForFunction(()=>document.querySelector('#tplot svg').viewBox.baseVal.height===document.querySelector('#tplot').clientHeight);
    captionBounds.push(await page.evaluate(()=>({caption:document.querySelector('#tmilestone').getBoundingClientRect().height,
      plot:document.querySelector('#tplot').clientHeight,svg:document.querySelector('#tplot svg').viewBox.baseVal.height})));
  }
  assert.ok(captionBounds[1].caption>captionBounds[0].caption,'The fixture exercises two different visible caption heights');
  assert.ok(captionBounds[1].plot<captionBounds[0].plot,'Longer captions reserve their actual layout height');
  await scrub.focus();await scrub.press('Home');await page.locator('#tmilestone').waitFor({state:'hidden'});
  await page.waitForFunction(()=>document.querySelector('#tplot svg').viewBox.baseVal.height===document.querySelector('#tplot').clientHeight);
  assert.ok(await page.locator('#tplot').evaluate(node=>node.clientHeight)>captionBounds[0].plot,'Hidden milestones return their space to the plot');
  report.layouts.push({label:'milestone caption reflow',width:320,captions:captionBounds});
  checked('At 320px, changing and hiding milestone captions rebuilds the SVG to the actual available plot height.');

  empty=true;longMilestones=false;await startDataRefresh(page);await waitForDataRefresh(page);
  await expectSummary('0 dated files','3 mapped projects');assert.equal(await page.locator('#tmode').isHidden(),true);
  assert.equal(await page.locator('#tdots [data-id],#tplot [data-hist]').count(),0);
  assert.match(await page.locator('#tsub').textContent(),/no|without/i);
  for(const width of [1440,320]){await page.setViewportSize({width,height:1000});await settle();await layout('no history '+width,width);await screenshot('timeline-no-history-'+width+'.png');}
  checked('A refreshed workspace without Git history explains its zero data and hides the unavailable commit mode.');
  assert.deepEqual(report.errors,[]);assert.deepEqual(report.writes,[]);
}catch(error){report.failure=error.stack;report.summary=await summary().textContent().catch(()=>null);report.context=await page.locator('#tsub').textContent().catch(()=>null);await page.screenshot({path:resolve(output,'failure.png')}).catch(()=>{});throw error;}
finally{await writeFile(resolve(output,'report.json'),JSON.stringify(report,null,2));await browser.close();}
