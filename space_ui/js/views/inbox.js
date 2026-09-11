/* Inbox tab: what arrived in the workspace, and whether anyone has dealt
   with it. Sessions starting, todos going blocked, repos shared with this
   workspace, GitHub issues from the mirror, items collected from polled
   connections, and anything an agent POSTs land as rows here (data: GET
   /api/inbox, a small service over ~/.quirq/inbox.json). Three
   statuses: new (unseen), seen (expanded once), done. Every field of a row
   is untrusted (agents write timeline content, anyone can POST), so every
   string is escaped before it reaches innerHTML. Independent of the other
   tabs: own fetch, own poll, own failure card. */
import {API_BASE,apiFetch} from '../core/api.js';
import {clearSlottedInterval,setSlottedInterval} from '../core/store.js';
import {toast} from '../core/ui.js';

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const dtfmt=iso=>{
  const t=iso?new Date(iso).getTime():NaN;
  return isFinite(t)?new Date(t).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}):'';
};
function rel(iso){
  if(!iso)return'';
  const s=(Date.now()-new Date(iso).getTime())/1000;
  if(!isFinite(s))return'';
  if(s<60)return'just now';
  if(s<3600)return Math.floor(s/60)+'m ago';
  if(s<86400)return Math.floor(s/3600)+'h ago';
  if(s<86400*30)return Math.floor(s/86400)+'d ago';
  return new Date(iso).toLocaleDateString(undefined,{dateStyle:'medium'});
}
/* the same three-way split every view uses: unreachable, unsupported, or
   the API's own words */
function failText(res){
  if(res.offline)return'xo-space is unreachable';
  if(res.notImplemented)return'not available for the active agent';
  return res.error||'request failed';
}
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
   Unseen count on the Inbox tab button. Started by app.js after the registry
   built the buttons; the view feeds it the counts it already has so a
   mutation never costs a second request. Never throws: a failed fetch leaves
   the tab exactly as it is. */
let lastBadge=null;
function paintBadge(n){
  const b=document.getElementById('tab-inbox');
  if(!b)return;
  n=Math.max(0,Math.floor(Number(n)||0));
  if(n===lastBadge)return; /* no DOM churn on an unchanged count */
  lastBadge=n;
  b.innerHTML=n>0?'Inbox<b class="inb-badge">'+n+'</b>':'Inbox';
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
export function initInboxBadge(){
  refreshInboxBadge();
  setSlottedInterval('inbox-badge',()=>refreshInboxBadge(),60000);
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
   fetches. "agents" is the catch-all for anything not written by a named
   feeder, so a row from a feeder added later still lands somewhere. */
const SOURCES=[['all','All'],['issues','Issues'],['connections','Connections'],
  ['workspace','Workspace'],['sharing','Sharing'],['agents','Agents']];
let srcFilter='all';
function sourceOf(it){
  const s=typeof it.source==='string'?it.source:'';
  if(s==='issues'||s==='connections'||s==='sharing')return s;
  if(s==='timeline'||s==='todos')return'workspace';
  return'agents';
}
const matchesSource=it=>srcFilter==='all'||sourceOf(it)===srcFilter;

/* ── connections section ──────────────────────────────────────────────────
   What the connections poller is watching (GET /api/connections), shown
   above the rows. Its own fetch, token and failure line: a slow or failed
   read here never delays or hides the inbox rows. These calls go same-origin
   like connectors.js does; they are not part of the /api/inbox family. */
let conns=null;             /* last good GET /api/connections payload */
let connsFailed=null;       /* last failed response, one muted line */
let connsOpen=null;         /* null = auto: open while any entry has an error */
let connsToken=0;
const connBusy=new Set();   /* toolkits with a Poll now in flight */

export default {
  id:'inbox',label:'Inbox',order:5,
  async mount(el,ctx){
    root=el;
    switchTo=ctx.switchTo;
    el.innerHTML='<div class="inb">'+head()+sources()+connsHTML()+skeleton()+'</div>';
    el.addEventListener('click',onClick);
    loadConns(); /* not awaited: independent of the rows, never blocks them */
    await load();
  },
  show(){
    /* mount just fetched; a return to the tab re-reads, then the poll keeps
       the list live while it is on screen */
    if(root&&Date.now()-lastLoad>4000)load();
    if(root)loadConns();
    setSlottedInterval('inbox-poll',load,30000);
  },
  hide(){clearSlottedInterval('inbox-poll');}
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
  render();
}

function render(){
  if(!root)return;
  const box=root.querySelector('.inb');
  if(box)box.innerHTML=head()+sources()+connsHTML()+body();
}
function summary(c){
  return c.new+' new · '+(c.new+c.seen)+' open · '+c.done+' done';
}
function head(){
  const c=counts();
  return'<div class="inb-head">'
    +'<span class="inb-eyebrow">Inbox</span>'
    +'<span class="inb-sum">'+(data?esc(summary(c)):'loading…')+'</span>'
    +'<span class="inb-spacer"></span>'
    +'<div class="inb-filter" role="group" aria-label="Filter inbox">'
      +FILTERS.map(([k,label])=>'<button type="button" data-filter="'+k+'"'
        +(filter===k?' class="is-on" aria-pressed="true"':' aria-pressed="false"')
        +'>'+label+'</button>').join('')
    +'</div>'
    +(c.new>0?'<button class="inb-btn" type="button" data-act="mark-all"'
      +(marking?' disabled':'')+' title="Mark every new item on this page as seen">Mark all seen</button>':'')
    +'<button class="inb-btn" type="button" data-act="refresh" title="Re-read the inbox">'
      +'&#8635; Refresh</button>'
  +'</div>';
}
/* the source pills, a second strip under the header */
function sources(){
  return'<div class="inb-src" role="group" aria-label="Filter by source">'
    +SOURCES.map(([k,label])=>'<button type="button" data-src="'+k+'"'
      +(srcFilter===k?' class="is-on" aria-pressed="true"':' aria-pressed="false"')
      +'>'+label+'</button>').join('')
  +'</div>';
}
function body(){
  if(!data&&failed)return'<div class="inb-fail">'+esc(failText(failed))+'</div>';
  if(!data||loadingRows)return skeleton();
  /* a failed load for a newly picked filter: the last good read belongs to
     another filter, so it must not be shown under this pill */
  if(failed&&dataFilter!==filter)return'<div class="inb-fail">'+esc(failText(failed))+'</div>';
  const all=data.items||[];
  const items=all.filter(matchesSource);
  const stale=failed?'<div class="inb-fail">'+esc(failText(failed))+' · showing the last good read</div>':'';
  if(!all.length)return stale+'<div class="inb-empty"><b>Nothing in the inbox.</b>'
    +'<p>Sessions, todos and shares arriving in the workspace land here.</p></div>';
  if(!items.length)return stale+'<div class="inb-empty"><b>Nothing from this source on this page.</b>'
    +'<p>The source pills filter the loaded page only. Pick All to see every row.</p></div>';
  return stale+'<div class="inb-rows">'+items.map(rowHTML).join('')+'</div>';
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
      +'<span class="inb-when" title="'+esc(dtfmt(it.ts))+'">'+esc(rel(it.ts))+'</span>'
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
function every(s){
  s=Number(s)||0;
  if(s>0&&s%3600===0)return'every '+(s/3600)+' h';
  return'every '+Math.max(1,Math.round(s/60))+' min';
}
function collectorLabels(c){
  const byId=new Map((Array.isArray(c.available_collectors)?c.available_collectors:[])
    .filter(a=>a&&typeof a==='object').map(a=>[a.id,a.label||a.id]));
  const ids=Array.isArray(c.collectors)?c.collectors:[];
  return ids.length?ids.map(id=>byId.get(id)||id).join(', '):'no collectors';
}
function connsHTML(){
  if(!conns){
    if(connsFailed)return'<div class="inb-conns"><div class="inb-conn-meta">Connections: '+esc(failText(connsFailed))+'</div></div>';
    return'<div class="inb-conns"><div class="inb-conn-meta">Loading connections…</div></div>';
  }
  const rows=polled();
  if(!rows.length)return'<div class="inb-conns"><div class="inb-conn-meta">'
    +'No connections polled yet. Connect a toolkit on the Connectors tab and turn on polling.</div></div>';
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
  const when=c.last_error
    ?'<span class="inb-conn-meta is-error" title="'+esc(c.last_error)+'">'+esc(c.last_error)+'</span>'
    :'<span class="inb-conn-meta">'+(c.last_poll_at?'last poll '+esc(rel(c.last_poll_at)):'never polled')+'</span>';
  return'<div class="inb-conn-row" data-toolkit="'+tk+'">'
    +'<b>'+esc(c.display_name||c.toolkit)+'</b>'
    +'<span class="inb-conn-meta">'+esc(collectorLabels(c))+' · '+esc(every(c.interval_s))
      +(c.enabled?'':' · polling off')+'</span>'
    +when
    +'<span class="inb-conn-acts">'
      +'<button class="inb-btn" type="button" data-act="conn-poll" data-toolkit="'+tk+'"'+off+'>Poll now</button>'
      +'<button class="inb-btn" type="button" data-act="conn-config" data-toolkit="'+tk+'">Configure</button>'
    +'</span>'
  +'</div>';
}

/* one delegated listener: rows are rebuilt on every paint, the listener is not */
function onClick(e){
  const b=e.target.closest('button[data-act],button[data-filter],button[data-src]');
  if(!b||b.disabled)return;
  if(b.dataset.filter){setFilter(b.dataset.filter);return;}
  if(b.dataset.src){setSource(b.dataset.src);return;}
  const id=b.dataset.id;
  switch(b.dataset.act){
    case'refresh':b.disabled=true;load();break;
    case'mark-all':markAllSeen();break;
    case'toggle':toggle(id);break;
    case'open':{const it=itemById(id);if(it)openLink(it);break;}
    case'done':setStatus(id,'done');break;
    case'reopen':setStatus(id,'seen');break;
    case'delete':remove(id);break;
    case'conns-toggle':connsOpen=!connsIsOpen();render();break;
    case'conn-poll':pollConn(b.dataset.toolkit);break;
    case'conn-config':switchTo('connectors');break;
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
  if(k===srcFilter||!SOURCES.some(([s])=>s===k))return;
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
/* Every new item on this page, one PATCH each, in order: the file is
   rewritten once per write, and ordered writes never race each other. A
   failed one does not stop the rest. */
async function markAllSeen(){
  if(marking||!data||dataFilter!==filter)return;
  const ids=(data.items||[]).filter(it=>it.status==='new').map(it=>it.id);
  if(!ids.length){toast('no new items in this list');return;}
  marking=true;render();
  let lost=0;
  for(const id of ids){
    const res=await apiFetch(API_BASE+'/api/inbox/'+encodeURIComponent(id),{method:'PATCH',body:{status:'seen'}});
    if(!res.ok)lost++;
  }
  marking=false;
  if(lost)toast(lost+' of '+ids.length+' could not be marked seen');
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
  const res=await apiFetch('/api/connections');
  if(mine!==connsToken)return; /* a newer read owns the section */
  if(res.ok&&res.data){conns=res.data;connsFailed=null;}
  else connsFailed=res;
  render();
}
/* Poll now: one POST, then both the section (new poll state) and the rows
   (what it collected) are re-read. The button stays disabled until then. */
async function pollConn(toolkit){
  if(typeof toolkit!=='string'||!toolkit||connBusy.has(toolkit))return;
  connBusy.add(toolkit);render();
  const res=await apiFetch('/api/connections/'+encodeURIComponent(toolkit)+'/poll',{method:'POST'});
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
