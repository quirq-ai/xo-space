/* Work tab: the Inbox page and Jobs share one mounted controller. The Inbox
   is the workspace's work items joined with their sessions: what a feeder
   ingested (a mail, a calendar event, a GitHub issue, a share) or an agent
   posted, and whether a session dealt with it (data: GET /api/inbox, one
   list of rows plus the sections summary; the item page is
   views/work-item.js). Tabs are the answer's sections, a tab groups its rows
   by entity, the state pills filter on the server. Every field of a row is
   untrusted (agents write titles, anyone can POST), so every string is
   escaped before it reaches innerHTML. Independent of the other tabs: own
   fetch, own poll, own failure card. The escape, the relative time, the pill
   strip and the failure wording come from core. Open lands on the item's own
   page through the hash (#/inbox/item?p=<project>&id=<item>), so a reload
   keeps the item; the polled apps and the connectors live in Setup. */
import {API_BASE,apiFetch,failText} from '../core/api.js';
import {clearSlottedInterval,setSlottedInterval} from '../core/store.js';
import {esc,pills,rel,toast} from '../core/ui.js';
import {openCommandResults} from '../core/command-results.js?v=20260914-results1';
import {describeOnce,describeSchedule,isScheduled,statusText} from '../core/jobs.js?v=20260916-jobs3';
import {INBOX_PAGES} from '../core/navigation.js?v=20260921-work2';

const dtfmt=iso=>{
  const t=iso?new Date(iso).getTime():NaN;
  return isFinite(t)?new Date(t).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}):'';
};

/* ── tab badge ─────────────────────────────────────────────────────────────
   The rows a person still has to look at (waiting plus new, summed over
   every section) on the Work tab button, appended beside the label the
   registry painted there (the label itself is never rewritten here).
   Started by app.js after the registry built the buttons; the view feeds it
   the sections summary it already has so a read never costs a second
   request, and its own 60 s poll rests while the Inbox page is on screen,
   where its 30 s read already carries the counts. Never throws: a failed
   fetch leaves the tab exactly as it is. */
let lastBadge=null;
let shown=false;            /* one of the Work pages is on screen */
function paintBadge(n){
  const b=document.getElementById('tab-inbox');
  if(!b)return;
  n=Math.max(0,Math.floor(Number(n)||0));
  if(n===lastBadge)return; /* no DOM churn on an unchanged count */
  lastBadge=n;
  let badge=b.querySelector('.inb-badge');
  if(n>0){
    if(!badge){badge=document.createElement('b');badge.className='inb-badge';b.appendChild(badge);}
    badge.textContent=String(n);
  }else if(badge)badge.remove();
}
/* waiting plus new over a sections list; every number coerced, never trusted */
export const needsYou=sections=>(Array.isArray(sections)?sections:[]).reduce((n,s)=>{
  const c=s&&s.counts&&typeof s.counts==='object'?s.counts:{};
  return n+Math.max(0,Number(c.waiting)||0)+Math.max(0,Number(c.new)||0);
},0);
export async function refreshInboxBadge(sections){
  try{
    if(!sections){
      const res=await apiFetch(API_BASE+'/api/inbox?state=open&limit=1');
      if(!res.ok||!res.data)return;
      sections=res.data.sections;
    }
    paintBadge(needsYou(sections));
  }catch(err){console.error('Inbox badge:',err);}
}
function startBadgePoll(){setSlottedInterval('inbox-badge',()=>refreshInboxBadge(),60000);}
export function initInboxBadge(){
  refreshInboxBadge();
  if(!shown||inboxPage!=='items')startBadgePoll(); /* Items polling supplies its own badge. */
}

/* ── view ─────────────────────────────────────────────────────────────── */
const STATES=[['open','Open'],['active','Active'],['waiting','Waiting'],['closed','Closed'],['all','All']];
/* The tabs are the answer's sections; this is only the order the page
   shows them in (a section the answer adds later lands after these). */
const TAB_ORDER=['projects','agents','connections','issues'];
const ROW_STATES=['new','running','waiting','failed','closed'];
const OUTCOME_LABEL={reply_drafted:'reply drafted',task_proposed:'task proposed',needs_you:'asks you',fyi:'fyi',handled:'handled'};
const EMPTY={
  open:['Nothing in the inbox.','Issues, polled apps, project shares and what agents post land here.'],
  active:['Nothing running.','No session is working on a row of this tab right now.'],
  waiting:['Nothing waiting for you.','Rows land here when a session drafted a reply, proposed a task or asked you something.'],
  closed:['Nothing closed yet.','Archived rows and handled items land here.'],
  all:['Nothing in the inbox.','Issues, polled apps, project shares and what agents post land here.'],
};
let root=null;
let switchTo=()=>{};        /* ctx.switchTo, captured on mount */
let inboxPage='items',inboxMount=null;
let section=TAB_ORDER[0];   /* the tab; the first answer confirms or corrects it */
let state='open';
let query='';
let data=null;              /* last good payload */
let dataKey='';             /* the tab and state that payload belongs to */
let failed=null;            /* last failed response, shown above the rows */
let loadingRows=false;      /* a tab or state change shows skeletons until it lands */
let token=0;                /* race guard: only the newest load may paint */
let lastLoad=0;

/* Jobs have their own read and DOM boundary. A jobs poll must never rebuild
   the Inbox rows or change the item-search scope. */
let jobs=null;
const jobRunBusy=new Set(); /* job ids with a Run now request in flight */
let jobsFailed=null;
let jobsToken=0;
let jobsLoading=false;
let jobsInFlight=false;
let jobsRefreshQueued=false;
let jobsPainted='';

export function createInboxViews(){
  return INBOX_PAGES.filter(page=>page.section==='inbox').map(page=>({
    ...page,
    toolbar:()=>page.id==='inbox-items'?inboxController.toolbar:null,
    mount(el,ctx){
      if(!inboxMount)inboxMount=inboxController.mount(el,ctx);
      return inboxMount;
    },
    show(){showInboxPage(page.route.split('/')[1]);},
    hide:hideInbox,
  }));
}

const inboxController={
  id:'inbox',label:'Work',order:5,
  toolbar:{search:{
    placeholder:'Search loaded inbox items…',
    getValue:()=>query,
    setValue(value){
      value=String(value??'');
      if(value===query)return;
      query=value;render();
    }
  }},
  async mount(el,ctx){
    root=el;
    switchTo=ctx.switchTo;
    el.innerHTML='<div class="inb"><header class="inb-page-head"><h1>Inbox</h1><div class="inb-page-actions"></div></header>'
      +'<section class="inb-items-page">'+tabsHTML()+head()+body()+'</section>'
      +'<section class="inb-jobs-page" hidden>'+jobsHTML()+'</section></div>';
    el.addEventListener('click',onClick);
    painted=paintKey();jobsPainted=jobsPaintKey();
  },
  show(){showInboxPage('items');},
  hide:hideInbox,
};

function showInboxPage(page){
  if(!root||!['items','jobs'].includes(page))return;
  inboxPage=page;shown=true;
  root.querySelector('.inb-page-head h1').textContent=INBOX_PAGES.find(item=>item.route==='inbox/'+page)?.label||'Inbox';
  for(const key of ['items','jobs'])root.querySelector('.inb-'+key+'-page').hidden=key!==page;
  clearSlottedInterval('inbox-poll');
  clearSlottedInterval('inbox-jobs-poll');
  if(page==='items'){
    clearSlottedInterval('inbox-badge');
    if(!data||Date.now()-lastLoad>4000)load();
    setSlottedInterval('inbox-poll',load,30000);
  }else{
    startBadgePoll();
    loadJobs();scheduleJobsPoll();
  }
}
function hideInbox(){
  shown=false;
  clearSlottedInterval('inbox-poll');
  clearSlottedInterval('inbox-jobs-poll');
  jobsToken++;jobsLoading=false;jobsRefreshQueued=false;
  /* An outstanding read stays tracked so reentry can queue a fresh request. */
  startBadgePoll();
}

const skeleton=()=>'<div class="inb-rows">'+'<div class="inb-skel"></div>'.repeat(4)+'</div>';
const readKey=()=>section+'|'+state;
/* the answer's sections in the page's order, unknown ones last */
function tabs(){
  const list=(data&&Array.isArray(data.sections)?data.sections:[]).filter(s=>s&&typeof s.id==='string');
  const rank=id=>{const i=TAB_ORDER.indexOf(id);return i<0?TAB_ORDER.length:i;};
  return list.slice().sort((a,b)=>rank(a.id)-rank(b.id));
}
const activeSection=()=>tabs().find(s=>s.id===section)||null;
const counts=()=>Object.assign({new:0,running:0,waiting:0,failed:0,closed:0},activeSection()&&activeSection().counts);
const rowsOfTab=()=>(data&&Array.isArray(data.rows)?data.rows:[]).filter(r=>r&&typeof r==='object'&&typeof r.id==='string'&&(!r.section||r.section===section));
const rowById=(id,kind)=>rowsOfTab().find(r=>r.id===id&&(kind==='session')===(r.kind==='session'));

async function load(){
  const mine=++token,key=readKey();
  const res=await apiFetch(API_BASE+'/api/inbox?section='+encodeURIComponent(section)+'&state='+encodeURIComponent(state)+'&limit=200');
  if(mine!==token)return; /* a newer load (tab or state change, refresh) owns the screen */
  lastLoad=Date.now();
  loadingRows=false;
  if(res.ok&&res.data){
    data=res.data;dataKey=key;failed=null;
    refreshInboxBadge(data.sections); /* the summary covers every section, not the tab */
    /* the answer names the tabs: a tab it does not carry is not a tab */
    const first=tabs()[0];
    if(first&&!tabs().some(s=>s.id===section)){section=first.id;loadingRows=true;render();load();return;}
  }else failed=res;
  if(paintKey()===painted){settle();return;} /* nothing new: leave focus and scroll alone */
  render();
}

/* ── painting ─────────────────────────────────────────────────────────────
   What the last paint was made from: the payload and every local flag the
   paint reads. A poll whose fresh read keys the same leaves the DOM alone,
   so keyboard focus and the scroll position survive the 30 s tick;
   anything else repaints with focus put back on the control that had it. */
let painted='';
const paintKey=()=>JSON.stringify([data,section,state,query,failed&&failText(failed),loadingRows]);
/* the focused control as a selector over the data-* it carries, so the same
   one can be found again once the rows are rebuilt */
function focusSelector(){
  const a=document.activeElement;
  if(!a||!root||!root.contains(a))return'';
  const keys=['act','id','kind','state','section','job'].filter(k=>a.dataset[k]!==undefined);
  return keys.map(k=>'[data-'+k+'="'+CSS.escape(a.dataset[k])+'"]').join('');
}
function render(){
  if(!root)return;
  const box=root.querySelector('.inb-items-page');
  if(!box)return;
  const sel=focusSelector();
  box.innerHTML=tabsHTML()+head()+body();
  painted=paintKey();
  if(sel){const el=box.querySelector(sel);if(el)el.focus({preventScroll:true});}
}
/* the parts that move without a repaint: the Refresh button, and the
   relative times, which an unchanged read still ages */
function settle(){
  const r=root.querySelector('button[data-act="refresh"]');
  if(r)r.disabled=false;
  root.querySelectorAll('[data-ts]').forEach(el=>{el.textContent=rel(el.dataset.ts);});
}
function summary(c){
  const parts=ROW_STATES.filter(k=>c[k]>0).map(k=>c[k]+' '+k);
  return parts.length?parts.join(' · '):'nothing here';
}
/* the tabs: one per section the answer carries, the waiting plus new count beside the label */
function tabsHTML(){
  const list=tabs();
  if(!list.length)return'';
  return'<div class="inb-tabs" role="tablist" aria-label="Inbox sections">'+list.map(s=>{
    const n=needsYou([s]),on=s.id===section;
    return'<button type="button" role="tab" data-section="'+esc(s.id)+'" aria-selected="'+(on?'true':'false')+'"'+(on?' class="is-on"':'')+'>'
      +esc(s.label||s.id)+(n>0?'<b>'+n+'</b>':'')+'</button>';
  }).join('')+'</div>';
}
function head(){
  return'<div class="inb-head">'
    +'<span class="inb-sum">'+(data?esc(summary(counts())):'loading…')+'</span>'
    +'<span class="inb-spacer"></span>'
    +pills(STATES,state,'state','Filter by state','inb-filter')
    +'<button class="inb-btn" type="button" data-act="refresh" title="Re-read the inbox">'
      +'&#8635; Refresh</button>'
  +'</div>';
}
/* the search terms against what a row shows */
const rowText=r=>[r.title,r.entity,r.state,r.kind,r.project_id,r.runtime,r.outcome&&r.outcome.kind,r.claim&&r.claim.runtime]
  .map(value=>String(value??'')).join(' ').toLowerCase();
function body(){
  if(!data&&failed)return'<div class="inb-fail">'+esc(failText(failed))+'</div>';
  if(!data||loadingRows)return skeleton();
  /* a failed load for a newly picked tab or state: the last good read
     belongs to another one, so it must not be shown under this pill */
  if(failed&&dataKey!==readKey())return'<div class="inb-fail">'+esc(failText(failed))+'</div>';
  const all=rowsOfTab();
  const terms=query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const rows=terms.length?all.filter(r=>{const text=rowText(r);return terms.every(term=>text.includes(term));}):all;
  const stale=failed?'<div class="inb-fail">'+esc(failText(failed))+' · showing the last good read</div>':'';
  const scope=terms.length?'<p class="inb-note" role="status">'+rows.length+' matching of '+all.length+' loaded items in this tab.</p>':'';
  const entities=activeSection()&&Array.isArray(activeSection().entities)?activeSection().entities:[];
  if(!all.length&&!entities.length){
    const [title,hint]=EMPTY[state]||EMPTY.open;
    return stale+'<div class="inb-empty"><b>'+esc(title)+'</b><p>'+esc(hint)+'</p></div>';
  }
  if(!rows.length&&terms.length)return stale+scope+'<div class="inb-empty"><b>No loaded inbox items match this search.</b>'
    +'<p>Try another term or clear the search. The tab and the state pill still apply.</p></div>';
  return stale+scope+groupsHTML(rows,entities,terms.length>0);
}
/* the tab's entity groups: the answer's entities in its order (a project,
   an agent, a toolkit, a repo, even with no rows), then any entity only the
   rows name; a search hides the groups it emptied */
function groupsHTML(rows,entities,searching){
  const byEntity=new Map();
  for(const r of rows){const key=String(r.entity??'');if(!byEntity.has(key))byEntity.set(key,[]);byEntity.get(key).push(r);}
  const groups=entities.filter(e=>e&&typeof e.id==='string').map(e=>({id:e.id,label:e.label||e.id,counts:e.counts,rows:byEntity.get(e.id)||[]}));
  const named=new Set(groups.map(g=>g.id));
  for(const [key,list] of byEntity)if(!named.has(key))groups.push({id:key,label:key||'no entity',counts:null,rows:list});
  return groups.filter(g=>g.rows.length||!searching).map(g=>{
    const c=g.counts&&typeof g.counts==='object'?Object.assign({new:0,running:0,waiting:0,failed:0,closed:0},g.counts):null;
    return'<section class="inb-group" aria-label="'+esc(g.label)+'">'
      +'<h2 class="inb-group-head"><span class="inb-group-name">'+esc(g.label)+'</span>'
        +'<span class="inb-group-counts">'+esc(c?summary(c):g.rows.length+' loaded')+'</span></h2>'
      +(g.rows.length?'<div class="inb-rows">'+g.rows.map(rowHTML).join('')+'</div>':'')
    +'</section>';
  }).join('');
}
/* one row, a button that opens the item page: dot · state · title · chips · when.
   A work item row shows its entity, its outcome kind and the runtime of the
   session that holds it now; a session row (a runtime session no work item
   owns) shows its runtime and time. */
function rowHTML(it){
  const isSession=it.kind==='session';
  const st=ROW_STATES.includes(it.state)?it.state:(isSession?'closed':'new');
  const live=!isSession&&it.claim&&typeof it.claim==='object'&&it.claim.live?it.claim:null;
  const runtime=isSession?it.runtime:(live?live.runtime:'');
  const outcome=!isSession&&it.outcome&&typeof it.outcome==='object'&&it.outcome.kind?(OUTCOME_LABEL[it.outcome.kind]||it.outcome.kind):'';
  const ts=it.updated_at||it.started_at||it.created_at||'';
  return'<div class="inb-row is-'+esc(st)+(isSession?' is-session':'')+'">'
    /* a real button: keyboard-reachable, and it says what it does */
    +'<button class="inb-row-head" type="button" data-act="open" data-id="'+esc(it.id)+'" data-kind="'+(isSession?'session':'workitem')+'" title="Open">'
      +'<i class="inb-dot" aria-hidden="true"></i>'
      +'<span class="inb-state">'+esc(st)+'</span>'
      +'<span class="inb-title">'+esc(it.title||it.id)+'</span>'
      +'<span class="inb-meta">'
        +(isSession?'<span class="inb-chip">session</span>':'')
        +(it.entity?'<span class="inb-chip is-entity">'+esc(it.entity)+'</span>':'')
        +(outcome?'<span class="inb-chip is-outcome">'+esc(outcome)+'</span>':'')
        +(runtime?'<span class="inb-chip is-runtime">'+esc(runtime)+'</span>':'')
      +'</span>'
      +'<span class="inb-when" data-ts="'+esc(ts)+'" title="'+esc(dtfmt(ts))+'">'+esc(rel(ts))+'</span>'
    +'</button>'
  +'</div>';
}

/* ── jobs (scheduled and manual) ───────────────────────────────────────── */
const jobsPaintKey=()=>JSON.stringify([jobs,jobsFailed&&failText(jobsFailed),[...jobRunBusy]]);
function jobRowHTML(job){
  const result=job.last_result;
  const duration=result?.duration_seconds;
  const elapsed=duration!=null&&Number.isFinite(Number(duration))?' · '+Number(duration).toFixed(2)+'s':'';
  const status=job.running?'Running'+(dtfmt(job.running_since)?' since '+dtfmt(job.running_since):'')
    :result?statusText(result.status)+(dtfmt(result.finished_at)?' · '+dtfmt(result.finished_at):'')+elapsed:'Not run yet';
  const tone=job.running?' is-running':result?.status==='ok'?' is-good':result?' is-error':'';
  const scheduled=isScheduled(job);
  const name=esc(job.name||job.id);
  const when=scheduled
    ?'<span>'+esc(describeSchedule(job))+'</span>'
      +'<span class="inb-job-enabled'+(job.enabled===false?' is-disabled':'')+'">'+(job.enabled===false?'Paused':'Enabled')+'</span>'
      +(dtfmt(job.next_run)?'<span>Next due '+esc(dtfmt(job.next_run))+'</span>':'')
    :'<span>'+esc(describeOnce(job,dtfmt))+'</span>';
  const runOff=job.running||jobRunBusy.has(job.id)?' disabled':'';
  return'<article class="inb-job-row" data-job-id="'+esc(job.id)+'">'
    +'<div class="inb-job-info"><div class="inb-job-title"><h3>'+name+'</h3>'
        +'<span class="inb-job-kind'+(scheduled?' is-scheduled':'')+'">'+(scheduled?'Repeating':'One time')+'</span></div>'
      +(job.description?'<p>'+esc(job.description)+'</p>':'')
      +'<div class="inb-job-meta">'+when+'</div>'
      +'<div class="inb-job-result'+tone+'" role="status">'+esc(status)+'</div>'
    +'</div><div class="inb-job-acts">'
      +'<button class="inb-btn" type="button" data-act="job-run" data-job="'+esc(job.id)+'" aria-label="Run '+name+' now"'+runOff+'>'+(job.running?'Running…':'Run now')+'</button>'
      +'<button class="inb-btn" type="button" data-act="job-results" data-job="'+esc(job.id)+'"'
        +' aria-label="Results for '+name+'">Results</button>'
    +'</div></article>';
}
function jobsHTML(){
  const failure=jobsFailed?'<p class="inb-jobs-state is-error" role="status">Could not load jobs: '
    +esc(failText(jobsFailed))+(jobs?' · showing the last good read':'')+'</p>':'';
  const content=jobs===null?(jobsFailed?'':'<p class="inb-jobs-state" role="status">Loading jobs…</p>')
    :jobs.length?jobs.map(jobRowHTML).join(''):'<p class="inb-jobs-state">No jobs yet. Create one in Setup → Jobs.</p>';
  return'<section class="inb-jobs" aria-labelledby="inb-jobs-title" aria-busy="'+jobsLoading+'">'
    +'<div class="inb-jobs-head"><h2 id="inb-jobs-title">Saved jobs'+(jobs?'<b>'+jobs.length+'</b>':'')+'</h2>'
      +'<div class="inb-jobs-actions"><button class="inb-btn" type="button" data-act="jobs-refresh" title="Re-read jobs">Refresh</button>'
        +'<button class="inb-btn" type="button" data-act="jobs-setup">Open Setup</button></div></div>'
    +failure+content+'</section>';
}
function renderJobs(){
  const section=root?.querySelector('.inb-jobs');
  if(!section)return;
  section.setAttribute('aria-busy',String(jobsLoading));
  const key=jobsPaintKey();
  if(key===jobsPainted)return;
  const sel=section.contains(document.activeElement)?focusSelector():'';
  section.outerHTML=jobsHTML();jobsPainted=key;
  if(sel)root.querySelector(sel)?.focus({preventScroll:true});
}
function scheduleJobsPoll(){
  clearSlottedInterval('inbox-jobs-poll');
  if(shown&&inboxPage==='jobs')setSlottedInterval('inbox-jobs-poll',()=>{if(!jobsInFlight)loadJobs();},
    jobs?.some(job=>job.running)?3000:30000);
}
async function loadJobs(){
  /* apiFetch shares concurrent GETs. A requested refresh during a slow read
     must run after it, rather than repainting its older snapshot as fresh. */
  if(jobsInFlight){jobsRefreshQueued=true;jobsToken++;return;}
  const mine=++jobsToken;
  jobsInFlight=true;jobsLoading=true;
  root?.querySelector('.inb-jobs')?.setAttribute('aria-busy','true');
  const res=await apiFetch(API_BASE+'/api/schedules');
  jobsInFlight=false;
  if(mine===jobsToken){
    if(res.ok&&Array.isArray(res.data?.jobs)){
      jobs=res.data.jobs.filter(job=>job&&typeof job==='object'&&typeof job.id==='string');
      jobsFailed=null;
    }else jobsFailed=res.ok?{error:'Unexpected jobs response'}:res;
  }
  if(jobsRefreshQueued){jobsRefreshQueued=false;await loadJobs();return;}
  jobsLoading=false;
  renderJobs();
  scheduleJobsPoll();
}
/* Run now is the Inbox's one job write. The started job replaces its row, and
   a read already in flight is discarded so it cannot repaint the old state. */
async function runJob(id){
  const job=jobs?.find(item=>item.id===id);
  if(!job||job.running||jobRunBusy.has(id))return;
  jobRunBusy.add(id);renderJobs();
  const res=await apiFetch(API_BASE+'/api/schedules/'+encodeURIComponent(id)+'/run',{method:'POST'});
  jobRunBusy.delete(id);
  if(res.ok&&res.data?.job){
    jobsToken++;
    jobs=jobs.map(item=>item.id===id?res.data.job:item);
    toast('Started '+(job.name||id));
  }else{
    toast(failText(res));
    if(res.status===409||res.status===404)loadJobs();
  }
  renderJobs();
  scheduleJobsPoll();
}

/* one delegated listener: rows are rebuilt on every paint, the listener is not */
function onClick(e){
  const b=e.target.closest('button[data-act],button[data-state],button[data-section]');
  if(!b||b.disabled)return;
  if(b.dataset.state){setState(b.dataset.state);return;}
  if(b.dataset.section){setSection(b.dataset.section);return;}
  switch(b.dataset.act){
    case'refresh':b.disabled=true;load();break;
    case'open':openRow(b.dataset.id,b.dataset.kind);break;
    case'jobs-refresh':loadJobs();break;
    case'jobs-setup':
      Promise.resolve(switchTo('setup/commands')).then(()=>{
        if(location.hash==='#/setup/commands')dispatchEvent(new CustomEvent('space:setup-section',{detail:{panel:'commands'}}));
      });
      break;
    case'job-run':runJob(b.dataset.job);break;
    case'job-results':{
      const job=jobs?.find(item=>item.id===b.dataset.job);
      if(job)openCommandResults({id:job.id,name:job.name||job.id});
      break;
    }
  }
}
/* a state pill or a tab fetches: the rows and the entity groups are the server's */
function setState(k){
  if(k===state||!STATES.some(([s])=>s===k))return;
  state=k;
  loadingRows=true;
  render();
  load();
}
function setSection(k){
  if(k===section||!tabs().some(s=>s.id===k))return;
  section=k;
  loadingRows=true;
  render();
  load();
}
/* Open: the item's own page, its transcript and the chat that continues it.
   The selection travels in the hash, so a reload or a Back keeps the item;
   a session row (no work item of its own) opens the transcript alone. */
function openRow(id,kind){
  const it=rowById(id,kind);
  if(!it)return;
  if(it.kind==='session')switchTo('inbox/item?s='+encodeURIComponent(it.id));
  else switchTo('inbox/item?p='+encodeURIComponent(it.project_id||'')+'&id='+encodeURIComponent(it.id));
}

export default inboxController;
