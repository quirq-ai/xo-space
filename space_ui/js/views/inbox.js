/* Inbox tab: what arrived in the workspace, and whether anyone has dealt
   with it. Sessions starting, todos going blocked, repos shared with this
   workspace, GitHub issues from the mirror, items collected from polled
   connections, and anything an agent POSTs land as rows here (data: GET
   /api/inbox, a small service over ~/.quirq/inbox.json). Three
   statuses: new (unseen), seen (expanded once), done. Every field of a row
   is untrusted (agents write timeline content, anyone can POST), so every
   string is escaped before it reaches innerHTML. Independent of the other
   tabs: own fetch, own poll, own failure card. The escape, the relative
   time, the pill strip and the failure wording come from core; the
   connections wording comes from core/connections.js, shared with the
   Connectors section so one payload never reads two ways. */
import {API_BASE,apiFetch,failText} from '../core/api.js';
import {clearSlottedInterval,setSlottedInterval} from '../core/store.js';
import {esc,pills,rel,toast} from '../core/ui.js';
import {collectorLabels,every,pollLine} from '../core/connections.js';
import {accountLabel} from '../core/connections.js';
import {openCommandResults} from '../core/command-results.js?v=20260914-results1';

const dtfmt=iso=>{
  const t=iso?new Date(iso).getTime():NaN;
  return isFinite(t)?new Date(t).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}):'';
};
/* The API validates links on write, but a hand-edited inbox.json reaches
   the page as-is until the next normalising write; never hand the previewer
   a path the file API would refuse anyway. */
const PROJ_RE=/^[A-Za-z0-9_:.\-]{1,200}$/;
const safePath=p=>typeof p==='string'&&p.length>0&&p.length<=500
  &&!p.startsWith('/')&&!p.includes('\\')&&!p.split('/').includes('..');
/* An item's url is checked here, before it ever reaches an href: a
   hand-edited events.jsonl or a hostile provider payload could carry a
   javascript: value. Only http(s) survives, and it is escaped on the way in. */
const safeUrl=u=>typeof u==='string'&&/^https?:\/\//i.test(u)?u:'';

/* ── tab badge ─────────────────────────────────────────────────────────────
   Unseen count on the Inbox tab button, appended beside the label the
   registry painted there (the label itself is never rewritten here).
   Started by app.js after the registry built the buttons; the view feeds it
   the counts it already has so a mutation never costs a second request, and
   its own 60 s poll rests while the view is on screen, where the view's
   30 s read already carries the counts. Never throws: a failed fetch leaves
   the tab exactly as it is. */
let lastBadge=null;
let shown=false;            /* the view is on screen: its own poll feeds the badge */
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
export async function refreshInboxBadge(counts){
  try{
    if(!counts){
      const res=await apiFetch(API_BASE+'/api/inbox?status=open&limit=1');
      if(!res.ok||!res.data)return;
      counts=res.data.counts||{};
    }
    paintBadge(counts.new);
  }catch(err){console.error('Inbox badge:',err);}
}
function startBadgePoll(){setSlottedInterval('inbox-badge',()=>refreshInboxBadge(),60000);}
export function initInboxBadge(){
  refreshInboxBadge();
  if(!shown)startBadgePoll(); /* a deep link may have shown the view already */
}

/* ── view ─────────────────────────────────────────────────────────────── */
const FILTERS=[['open','Open'],['done','Done'],['all','All']];
let root=null;
let switchTo=()=>{};        /* ctx.switchTo, captured on mount */
let filter='open';
let data=null;              /* last good payload */
let dataFilter='';          /* the filter that payload belongs to */
let failed=null;            /* last failed response, shown above the rows */
let loadingRows=false;      /* a filter change shows skeletons until it lands */
let token=0;                /* race guard: only the newest load may paint */
let lastLoad=0;
let marking=false;          /* "Mark all seen" in flight */
const expanded=new Set();   /* ids with the body open */
const busy=new Set();       /* ids with a write in flight */

/* ── source filter ────────────────────────────────────────────────────────
   Client-side over the loaded page (limit 200): picking a source never
   fetches. One row per pill, naming the feeder sources it covers, so adding
   a feeder is one entry here. "agents" is the catch-all for anything not
   written by a named feeder, so a row from a feeder added later still lands
   somewhere. */
const SOURCES=[
  {id:'all',label:'All',sources:[]},
  {id:'issues',label:'Issues',sources:['issues']},
  {id:'connections',label:'Connections',sources:['connections']},
  {id:'workspace',label:'Workspace',sources:['timeline','todos']},
  {id:'sharing',label:'Sharing',sources:['sharing']},
  {id:'agents',label:'Agents',sources:[]},
];
const SOURCE_PILLS=SOURCES.map(s=>[s.id,s.label]);
let srcFilter='all';
let query='';
function sourceOf(it){
  const s=typeof it.source==='string'?it.source:'';
  const row=SOURCES.find(r=>r.sources.includes(s));
  return row?row.id:'agents';
}
const matchesSource=it=>srcFilter==='all'||sourceOf(it)===srcFilter;

/* ── connections section ──────────────────────────────────────────────────
   What the connections poller is watching (GET /api/connections), shown
   above the rows. Its own fetch, token and failure line: a slow or failed
   read here never delays or hides the inbox rows. Same API_BASE as every
   other call on this page; they are not part of the /api/inbox family. */
let conns=null;             /* last good GET /api/connections payload */
let connsFailed=null;       /* last failed response, one muted line */
let connsOpen=null;         /* null = auto: open while any entry has an error */
let connsToken=0;
const connBusy=new Set();   /* toolkits with a Poll now in flight */

/* Scheduled commands have their own read and DOM boundary. A jobs poll must
   never rebuild an expanded Inbox body or change the item-search scope. */
let jobs=null;
let jobsFailed=null;
let jobsToken=0;
let jobsLoading=false;
let jobsInFlight=false;
let jobsRefreshQueued=false;
let jobsPainted='';

export default {
  id:'inbox',label:'Inbox',order:5,
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
    el.innerHTML='<div class="inb">'+head()+sources()+connsHTML()+jobsHTML()+skeleton()+'</div>';
    el.addEventListener('click',onClick);
    loadConns(); /* not awaited: independent of the rows, never blocks them */
    loadJobs();
    await load();
  },
  show(){
    /* mount just fetched; a return to the tab re-reads, then the poll keeps
       the list live while it is on screen. The badge poll rests meanwhile:
       every read here paints the badge from its own counts. */
    shown=true;
    clearSlottedInterval('inbox-badge');
    if(root&&Date.now()-lastLoad>4000)load();
    if(root)loadConns();
    if(root&&!jobsLoading)loadJobs();
    scheduleJobsPoll();
    setSlottedInterval('inbox-poll',load,30000);
  },
  hide(){
    shown=false;
    clearSlottedInterval('inbox-poll');
    clearSlottedInterval('inbox-jobs-poll');
    jobsToken++;jobsLoading=false;jobsRefreshQueued=false;
    /* An outstanding read stays tracked so reentry can queue a fresh request. */
    startBadgePoll();
  }
};

const skeleton=()=>'<div class="inb-rows">'+'<div class="inb-skel"></div>'.repeat(4)+'</div>';
const counts=()=>Object.assign({new:0,seen:0,done:0},data&&data.counts);
const itemById=id=>data&&(data.items||[]).find(it=>it.id===id);

async function load(){
  const mine=++token;
  const res=await apiFetch(API_BASE+'/api/inbox?status='+encodeURIComponent(filter)+'&limit=200');
  if(mine!==token)return; /* a newer load (filter change, refresh) owns the screen */
  lastLoad=Date.now();
  loadingRows=false;
  if(res.ok&&res.data){
    data=res.data;dataFilter=filter;failed=null;
    const ids=new Set((data.items||[]).map(it=>it.id));
    for(const id of [...expanded])if(!ids.has(id))expanded.delete(id);
    refreshInboxBadge(data.counts); /* counts cover the whole file, not the filter */
  }else failed=res;
  if(paintKey()===painted){settle();return;} /* nothing new: leave focus and scroll alone */
  render();
}

/* ── painting ─────────────────────────────────────────────────────────────
   What the last paint was made from: the payload and every local flag the
   paint reads. A poll whose fresh read keys the same leaves the DOM alone,
   so keyboard focus and the scroll inside an expanded body survive the
   30 s tick; anything else repaints with focus put back on the control
   that had it. */
let painted='';
const paintKey=()=>JSON.stringify([data,filter,srcFilter,query,failed&&failText(failed),loadingRows,marking,
  [...expanded],conns,connsFailed&&failText(connsFailed),connsOpen,[...connBusy]]);
/* the focused control as a selector over the data-* it carries, so the same
   one can be found again once the rows are rebuilt */
function focusSelector(){
  const a=document.activeElement;
  if(!a||!root||!root.contains(a))return'';
  const keys=['act','id','toolkit','filter','src','job'].filter(k=>a.dataset[k]!==undefined);
  return keys.map(k=>'[data-'+k+'="'+CSS.escape(a.dataset[k])+'"]').join('');
}
function render(){
  if(!root)return;
  const box=root.querySelector('.inb');
  if(!box)return;
  const sel=focusSelector();
  box.innerHTML=head()+sources()+connsHTML()+jobsHTML()+body();
  jobsPainted=jobsPaintKey();
  painted=paintKey();
  if(sel){const el=box.querySelector(sel);if(el)el.focus({preventScroll:true});}
}
/* the parts that move without a repaint: buttons a write disabled, the
   Refresh button, and the relative times, which an unchanged read still ages */
function settle(){
  syncBusy();
  const r=root.querySelector('button[data-act="refresh"]');
  if(r)r.disabled=false;
  root.querySelectorAll('[data-ts]').forEach(el=>{el.textContent=rel(el.dataset.ts);});
}
function summary(c){
  return c.new+' new · '+(c.new+c.seen)+' open · '+c.done+' done';
}
function head(){
  const c=counts();
  const narrowed=query.trim()||srcFilter!=='all';
  return'<div class="inb-head">'
    +'<span class="inb-eyebrow">Inbox</span>'
    +'<span class="inb-sum">'+(data?esc(summary(c)):'loading…')+'</span>'
    +'<span class="inb-spacer"></span>'
    +pills(FILTERS,filter,'filter','Filter inbox','inb-filter')
    +(c.new>0?'<button class="inb-btn" type="button" data-act="mark-all"'
      +(marking?' disabled':'')+' title="'+(narrowed
        ?'Mark every new item in the loaded status page as seen, including items hidden by search or source filters'
        :'Mark every new item on this page as seen')+'">'+(narrowed?'Mark all loaded seen':'Mark all seen')+'</button>':'')
    +'<button class="inb-btn" type="button" data-act="refresh" title="Re-read the inbox">'
      +'&#8635; Refresh</button>'
  +'</div>';
}
/* the source pills, a second strip under the header */
function sources(){
  return pills(SOURCE_PILLS,srcFilter,'src','Filter by source','inb-src');
}
function body(){
  if(!data&&failed)return'<div class="inb-fail">'+esc(failText(failed))+'</div>';
  if(!data||loadingRows)return skeleton();
  /* a failed load for a newly picked filter: the last good read belongs to
     another filter, so it must not be shown under this pill */
  if(failed&&dataFilter!==filter)return'<div class="inb-fail">'+esc(failText(failed))+'</div>';
  const all=data.items||[];
  const sourceItems=all.filter(matchesSource);
  const terms=query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const items=terms.length?sourceItems.filter(it=>{
    const sourceLabel=SOURCES.find(source=>source.id===sourceOf(it))?.label;
    const text=[it.title,it.body,it.kind,it.source,sourceLabel,it.project_id]
      .map(value=>String(value??'')).join(' ').toLowerCase();
    return terms.every(term=>text.includes(term));
  }):sourceItems;
  const stale=failed?'<div class="inb-fail">'+esc(failText(failed))+' · showing the last good read</div>':'';
  const scope=terms.length||srcFilter!=='all'?'<p class="inb-note" role="status">'
    +items.length+' matching of '+all.length+' loaded items in this status page.'
    +(srcFilter!=='all'?' '+sourceItems.length+' in the selected source.':'')
    +(counts().new>0?' Mark all loaded seen includes items hidden by search or source filters.':'')+'</p>':'';
  if(!all.length)return stale+scope+'<div class="inb-empty"><b>Nothing in the inbox.</b>'
    +'<p>Sessions, todos and shares arriving in the workspace land here.</p></div>';
  if(!sourceItems.length)return stale+scope+'<div class="inb-empty"><b>Nothing from this source on this page.</b>'
    +'<p>The source pills filter the loaded page only. Pick All to see every row.</p></div>';
  if(!items.length)return stale+scope+'<div class="inb-empty"><b>No loaded inbox items match this search.</b>'
    +'<p>Try another term or clear the search. Status and source filters still apply.</p></div>';
  return stale+scope+'<div class="inb-rows">'+items.map(rowHTML).join('')+'</div>';
}
const hasLink=it=>!!it.link&&typeof it.link==='object'&&!!(it.link.view||it.link.project);
function rowHTML(it){
  const open=expanded.has(it.id);
  const id=esc(it.id);
  const done=it.status==='done';
  const off=busy.has(it.id)?' disabled':'';
  const url=safeUrl(it.url);
  return'<div class="inb-row is-'+esc(it.status)+(open?' is-open':'')+'" id="inb-row-'+id+'">'
    /* a real button: keyboard-reachable, and it says what it does */
    +'<button class="inb-row-head" type="button" data-act="toggle" data-id="'+id+'" '
      +'aria-expanded="'+(open?'true':'false')+'" aria-controls="inb-body-'+id+'">'
      +'<i class="inb-dot" aria-hidden="true"></i>'
      +'<span class="inb-kind">'+esc(it.kind)+'</span>'
      +'<span class="inb-title">'+esc(it.title)+'</span>'
      +(it.project_id?'<span class="inb-proj">'+esc(it.project_id)+'</span>':'')
      +'<span class="inb-when" data-ts="'+esc(it.ts)+'" title="'+esc(dtfmt(it.ts))+'">'+esc(rel(it.ts))+'</span>'
    +'</button>'
    +(open?'<div class="inb-body" id="inb-body-'+id+'">'
      +(it.body?'<pre class="inb-text">'+esc(it.body)+'</pre>':'<div class="inb-note">no details</div>')
      +'<div class="inb-actions">'
        +(hasLink(it)?'<button class="inb-btn" type="button" data-act="open" data-id="'+id+'">Open</button>':'')
        /* a real link, and only for an http(s) url: safeUrl ran above */
        +(url?'<a class="inb-btn" href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">Open link</a>':'')
        +'<button class="inb-btn" type="button" data-act="'+(done?'reopen':'done')+'" data-id="'+id+'"'+off+'>'
          +(done?'Reopen':'Done')+'</button>'
        +'<button class="inb-btn is-danger" type="button" data-act="delete" data-id="'+id+'"'+off+'>Delete</button>'
      +'</div>'
    +'</div>':'')
  +'</div>';
}

/* ── connections section rendering ─────────────────────────────────────── */
const polled=()=>((conns&&conns.connections)||[]).filter(c=>c&&typeof c==='object'&&(c.configured||c.connected_here));
const connsIsOpen=()=>connsOpen===null?polled().some(c=>c.last_error):connsOpen;
function connsHTML(){
  if(!conns){
    if(connsFailed)return'<div class="inb-conns"><div class="inb-conn-meta">Connections: '+esc(failText(connsFailed))+'</div></div>';
    return'<div class="inb-conns"><div class="inb-conn-meta">Loading connections…</div></div>';
  }
  const rows=polled();
  if(!rows.length)return'<div class="inb-conns"><div class="inb-conn-meta">'
    +'No updates yet. Open Setup → Connectors to connect an app and turn on polling.</div></div>';
  const errors=rows.filter(c=>c.last_error).length;
  const open=connsIsOpen();
  return'<div class="inb-conns'+(open?' is-open':'')+'">'
    +'<button class="inb-conns-head" type="button" data-act="conns-toggle" aria-expanded="'+(open?'true':'false')+'">'
      +'<i aria-hidden="true">'+(open?'&#9662;':'&#9656;')+'</i>'
      +'<span>Connections</span><b>'+rows.length+'</b>'
      +(errors?'<em>'+errors+' with errors</em>':'')
      +(conns.poller_enabled===false?'<em>poller off</em>':'')
      +(conns.signed_in===false?'<em>not signed in</em>':'')
    +'</button>'
    +(open?rows.map(connRowHTML).join(''):'')
  +'</div>';
}
function connRowHTML(c){
  const tk=esc(c.toolkit);
  const off=connBusy.has(c.toolkit)?' disabled':'';
  const line=pollLine(c);
  const when=line.error
    ?'<span class="inb-conn-meta is-error" title="'+esc(line.error)+'">'+esc(line.error)+'</span>'
    :'<span class="inb-conn-meta">'+esc(line.text)+'</span>';
  /* the account the session is bound to (an email, once the server has
     resolved it) sits inside the name cell so the row's grid keeps its
     four columns */
  const acct=accountLabel(c);
  return'<div class="inb-conn-row" data-toolkit="'+tk+'">'
    +'<b>'+esc(c.display_name||c.toolkit)
      +(acct?'<span class="inb-conn-acct">'+esc(acct)+'</span>':'')+'</b>'
    +'<span class="inb-conn-meta">'+esc(collectorLabels(c))+' · '+esc(every(c.interval_s))
      +(c.enabled?'':' · polling off')+'</span>'
    +when
    +'<span class="inb-conn-acts">'
      +'<button class="inb-btn" type="button" data-act="conn-poll" data-toolkit="'+tk+'"'+off+'>Poll now</button>'
      +'<button class="inb-btn" type="button" data-act="conn-config" data-toolkit="'+tk+'">Configure</button>'
    +'</span>'
  +'</div>';
}

/* ── scheduled jobs ────────────────────────────────────────────────────── */
const jobsPaintKey=()=>JSON.stringify([jobs,jobsFailed&&failText(jobsFailed)]);
function jobInterval(seconds){
  const n=Number(seconds);
  if(!Number.isFinite(n)||n<=0)return'Interval unavailable';
  for(const [unit,size] of [['d',86400],['h',3600],['min',60]]){
    if(n%size===0)return'Every '+(n/size)+' '+unit;
  }
  return'Every '+n+' s';
}
function jobRowHTML(job){
  const result=job.last_result;
  const duration=result?.duration_seconds;
  const elapsed=duration!=null&&Number.isFinite(Number(duration))?' · '+Number(duration).toFixed(2)+'s':'';
  const status=job.running?'Running'+(dtfmt(job.running_since)?' since '+dtfmt(job.running_since):'')
    :result?String(result.status||'Unknown result')+(dtfmt(result.finished_at)?' · '+dtfmt(result.finished_at):'')+elapsed:'Not run yet';
  const tone=job.running?' is-running':result?.status==='ok'?' is-good':result?' is-error':'';
  return'<article class="inb-job-row" data-job-id="'+esc(job.id)+'">'
    +'<div class="inb-job-info"><h3>'+esc(job.name||job.id)+'</h3>'
      +(job.description?'<p>'+esc(job.description)+'</p>':'')
      +'<div class="inb-job-meta"><span>'+esc(jobInterval(job.every_seconds))+'</span>'
        +'<span class="inb-job-enabled'+(job.enabled===false?' is-disabled':'')+'">'+(job.enabled===false?'Disabled':'Enabled')+'</span>'
        +(dtfmt(job.next_run)?'<span>Next due '+esc(dtfmt(job.next_run))+'</span>':'')+'</div>'
      +'<div class="inb-job-result'+tone+'" role="status">'+esc(status)+'</div>'
    +'</div><button class="inb-btn" type="button" data-act="job-results" data-job="'+esc(job.id)+'"'
      +' aria-label="Results for '+esc(job.name||job.id)+'">Results</button>'
  +'</article>';
}
function jobsHTML(){
  const failure=jobsFailed?'<p class="inb-jobs-state is-error" role="status">Could not load scheduled jobs: '
    +esc(failText(jobsFailed))+(jobs?' · showing the last good read':'')+'</p>':'';
  const content=jobs===null?(jobsFailed?'':'<p class="inb-jobs-state" role="status">Loading scheduled jobs…</p>')
    :jobs.length?jobs.map(jobRowHTML).join(''):'<p class="inb-jobs-state">No scheduled jobs. Add an interval to a command in Setup.</p>';
  return'<section class="inb-jobs" aria-labelledby="inb-jobs-title" aria-busy="'+jobsLoading+'">'
    +'<div class="inb-jobs-head"><h2 id="inb-jobs-title">Jobs'+(jobs?'<b>'+jobs.length+'</b>':'')+'</h2>'
      +'<div class="inb-jobs-actions"><button class="inb-btn" type="button" data-act="jobs-refresh" title="Re-read scheduled jobs">Refresh</button>'
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
  if(shown)setSlottedInterval('inbox-jobs-poll',()=>{if(!jobsInFlight)loadJobs();},
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
      jobs=res.data.jobs.filter(job=>job&&typeof job==='object'&&typeof job.id==='string'&&job.every_seconds!=null);
      jobsFailed=null;
    }else jobsFailed=res.ok?{error:'Unexpected jobs response'}:res;
  }
  if(jobsRefreshQueued){jobsRefreshQueued=false;await loadJobs();return;}
  jobsLoading=false;
  renderJobs();
  scheduleJobsPoll();
}

/* one delegated listener: rows are rebuilt on every paint, the listener is not */
function onClick(e){
  const b=e.target.closest('button[data-act],button[data-filter],button[data-src]');
  if(!b||b.disabled)return;
  if(b.dataset.filter){setFilter(b.dataset.filter);return;}
  if(b.dataset.src){setSource(b.dataset.src);return;}
  const id=b.dataset.id;
  switch(b.dataset.act){
    case'refresh':b.disabled=true;load();loadJobs();break;
    case'mark-all':markAllSeen();break;
    case'toggle':toggle(id);break;
    case'open':{const it=itemById(id);if(it)openLink(it);break;}
    case'done':setStatus(id,'done');break;
    case'reopen':setStatus(id,'seen');break;
    case'delete':remove(id);break;
    case'conns-toggle':connsOpen=!connsIsOpen();render();break;
    case'conn-poll':pollConn(b.dataset.toolkit);break;
    case'conn-config':switchTo('connectors');break;
    case'jobs-refresh':loadJobs();break;
    case'jobs-setup':
      Promise.resolve(switchTo('secrets')).then(()=>dispatchEvent(new CustomEvent('space:setup-section',{detail:{panel:'commands'}})));
      break;
    case'job-results':{
      const job=jobs?.find(item=>item.id===b.dataset.job);
      if(job)openCommandResults({id:job.id,name:job.name||job.id});
      break;
    }
  }
}
function setFilter(k){
  if(k===filter||!FILTERS.some(([f])=>f===k))return;
  filter=k;
  loadingRows=true;
  render();
  load();
}
/* a source pill only repaints: the page is already here */
function setSource(k){
  if(k===srcFilter||!SOURCES.some(s=>s.id===k))return;
  srcFilter=k;
  render();
}
/* Expanding a new item is the act of seeing it: one PATCH, only while it is
   still new, and the local copy flips first so a collapse and re-expand
   before the re-read cannot fire a second one. A failed PATCH flips it back
   and repaints, so the row reads as new again and the next expand retries. */
async function toggle(id){
  const it=itemById(id);
  if(!it)return;
  if(expanded.has(id))expanded.delete(id);else expanded.add(id);
  render();
  const row=document.getElementById('inb-row-'+id);
  if(row)row.querySelector('.inb-row-head').focus({preventScroll:true});
  if(!expanded.has(id)||it.status!=='new'||busy.has(id))return;
  it.status='seen';
  const ok=await setStatus(id,'seen');
  /* on failure `it` may be stale if a poll landed meanwhile; reverting a
     stale copy is harmless and the repaint shows whatever is current */
  if(!ok&&it.status==='seen'){it.status='new';render();}
}
/* resolves true once the write landed and the list was re-read */
async function setStatus(id,status){
  if(busy.has(id))return false;
  busy.add(id);syncBusy();
  const res=await apiFetch(API_BASE+'/api/inbox/'+encodeURIComponent(id),{method:'PATCH',body:{status}});
  busy.delete(id);
  if(!res.ok){toast('update failed: '+failText(res));syncBusy();return false;}
  await load();
  return true;
}
async function remove(id){
  if(busy.has(id))return;
  busy.add(id);syncBusy();
  const res=await apiFetch(API_BASE+'/api/inbox/'+encodeURIComponent(id),{method:'DELETE'});
  busy.delete(id);
  if(!res.ok){toast('delete failed: '+failText(res));syncBusy();return;}
  expanded.delete(id);
  await load();
}
/* a paint mid-flight must not re-enable a pressed button */
function syncBusy(){
  root.querySelectorAll('button[data-act][data-id]').forEach(b=>{
    if(b.dataset.act!=='toggle'&&b.dataset.act!=='open')b.disabled=busy.has(b.dataset.id);
  });
}
/* Every new item on this page in one PATCH /api/inbox {ids, status}: the
   file is rewritten once for the whole batch, and the reply says how many
   actually changed and which ids were gone by then. A failed request
   changes nothing here and says why in the same words as every other
   failed write. */
async function markAllSeen(){
  if(marking||!data||dataFilter!==filter)return;
  const ids=(data.items||[]).filter(it=>it.status==='new').map(it=>it.id);
  if(!ids.length){toast('no new items in this list');return;}
  marking=true;render();
  const res=await apiFetch(API_BASE+'/api/inbox',{method:'PATCH',body:{ids,status:'seen'}});
  marking=false;
  if(!res.ok)toast('mark all seen failed: '+failText(res));
  else{
    const missing=res.data&&Array.isArray(res.data.missing)?res.data.missing.length:0;
    if(missing)toast(missing+' of '+ids.length+' were already gone');
  }
  await load();
}
/* Open: a file link previews it in the Files tab; a view link jumps there;
   a bare project link lands on the Files list. switchTo is not awaited: its
   tab and event side effects are synchronous, and the previewer closes on
   any non-Files view, so the switch must happen before the preview event.
   Unknown view ids are ignored by the registry itself. */
function openLink(it){
  const l=it.link;
  if(!l||typeof l!=='object')return;
  const project=typeof l.project==='string'&&PROJ_RE.test(l.project)?l.project:'';
  if(project&&safePath(l.path)){
    switchTo('projects');
    dispatchEvent(new CustomEvent('space:preview-file',{detail:{project,path:l.path}}));
    return;
  }
  if(typeof l.view==='string'&&l.view){switchTo(l.view);return;}
  if(project)switchTo('projects');
}

/* ── connections section data ──────────────────────────────────────────── */
async function loadConns(){
  const mine=++connsToken;
  const res=await apiFetch(API_BASE+'/api/connections');
  if(mine!==connsToken)return; /* a newer read owns the section */
  if(res.ok&&res.data){conns=res.data;connsFailed=null;}
  else connsFailed=res;
  if(paintKey()===painted)return; /* the section reads the same: no repaint */
  render();
}
/* Poll now: one POST, then both the section (new poll state) and the rows
   (what it collected) are re-read. The button stays disabled until then. */
async function pollConn(toolkit){
  if(typeof toolkit!=='string'||!toolkit||connBusy.has(toolkit))return;
  connBusy.add(toolkit);render();
  const res=await apiFetch(API_BASE+'/api/connections/'+encodeURIComponent(toolkit)+'/poll',{method:'POST'});
  connBusy.delete(toolkit);
  if(!res.ok)toast('poll failed: '+failText(res));
  else{
    const r=res.data||{};
    if(r.skipped)toast('poll skipped: '+String(r.skipped));
    else if(r.error)toast('poll failed: '+String(r.error));
    else toast((Number(r.new_events)||0)+' new from '+toolkit);
  }
  await Promise.all([loadConns(),load()]);
}
