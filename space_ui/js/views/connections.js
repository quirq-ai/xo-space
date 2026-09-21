/* Setup > Connections: what the connections poller watches (GET
   /api/connections, one row per polled app with Poll now), above the
   Connectors controller (the Composio key, account apps and workspace
   integrations; views/connectors.js). One mounted controller: Setup mounts it
   into its Connections panel on the section's first visit and calls show and
   hide as the panel comes and goes, so the 30 s read runs only while the
   section is on screen. Its own fetch, token and failure line: a slow or
   failed read never blocks the connector cards. Every server string is
   escaped before it reaches innerHTML; a row's wording comes from
   core/connections.js, shared with the Polling drawer so one payload never
   reads two ways. */
import {API_BASE,apiFetch,failText} from '../core/api.js';
import {esc,toast} from '../core/ui.js';
import {accountLabel,collectorLabels,every,pollLine} from '../core/connections.js';
import {clearSlottedInterval,setSlottedInterval} from '../core/store.js';
import connectorsView from './connectors.js?v=20260921-work2';

let root=null;
let conns=null;             /* last good GET /api/connections payload */
let connsFailed=null;       /* last failed response, one muted line */
let connsToken=0;           /* race guard: only the newest read may paint */
let painted='';
const connBusy=new Set();   /* toolkits with a Poll now in flight */

const polled=()=>((conns&&conns.connections)||[]).filter(c=>c&&typeof c==='object'&&(c.configured||c.connected_here));
const paintKey=()=>JSON.stringify([conns,connsFailed&&failText(connsFailed),[...connBusy]]);

function rowHTML(c){
  const tk=esc(c.toolkit);
  const off=connBusy.has(c.toolkit)?' disabled':'';
  const line=pollLine(c);
  const when=line.error
    ?'<span class="conn-polled-meta is-error" title="'+esc(line.error)+'">'+esc(line.error)+'</span>'
    :'<span class="conn-polled-meta">'+esc(line.text)+'</span>';
  /* the account the session is bound to (an email, once the server has
     resolved it) sits inside the name cell so the row keeps its four columns */
  const acct=accountLabel(c);
  return'<div class="conn-polled-row" data-toolkit="'+tk+'">'
    +'<b>'+esc(c.display_name||c.toolkit)
      +(acct?'<span class="conn-polled-acct">'+esc(acct)+'</span>':'')+'</b>'
    +'<span class="conn-polled-meta">'+esc(collectorLabels(c))+' · '+esc(every(c.interval_s))
      +(c.enabled?'':' · polling off')+'</span>'
    +when
    +'<span class="conn-polled-acts">'
      +'<button class="conn-secondary" type="button" data-act="conn-poll" data-toolkit="'+tk+'"'+off+'>Poll now</button>'
      +'<button class="conn-secondary" type="button" data-act="conn-config" data-toolkit="'+tk+'">Configure</button>'
    +'</span>'
  +'</div>';
}
function polledHTML(){
  if(!conns){
    if(connsFailed)return'<p class="conn-polled-state">Connections: '+esc(failText(connsFailed))+'</p>';
    return'<p class="conn-polled-state">Loading connections…</p>';
  }
  const rows=polled();
  if(!rows.length)return'<p class="conn-polled-state">No updates yet. Connect an app below and turn on polling.</p>';
  const errors=rows.filter(c=>c.last_error).length;
  const notes=[rows.length+(rows.length===1?' polled app':' polled apps'),errors?errors+' with errors':'',
    conns.poller_enabled===false?'poller off':'',conns.signed_in===false?'not signed in':''].filter(Boolean);
  return'<p class="conn-polled-summary">'+esc(notes.join(' · '))+'</p>'+rows.map(rowHTML).join('');
}
/* the focused control as a selector over the data-* it carries, so the same
   one can be found again once the rows are rebuilt */
function focusSelector(){
  const a=document.activeElement;
  if(!a||!root||!root.contains(a)||!a.dataset)return'';
  return ['act','toolkit'].filter(k=>a.dataset[k]!==undefined)
    .map(k=>'[data-'+k+'="'+CSS.escape(a.dataset[k])+'"]').join('');
}
function renderPolled(){
  const box=root?.querySelector('[data-polled]');
  if(!box)return;
  const key=paintKey();if(key===painted)return;
  const sel=focusSelector();
  box.innerHTML=polledHTML();painted=key;
  if(sel)box.querySelector(sel)?.focus({preventScroll:true});
}
async function loadConns(){
  const mine=++connsToken;
  const res=await apiFetch(API_BASE+'/api/connections');
  if(mine!==connsToken)return; /* a newer read owns the section */
  if(res.ok&&res.data){conns=res.data;connsFailed=null;}
  else connsFailed=res;
  renderPolled();
}
/* Poll now: one POST, then the section is re-read for the new poll state.
   The button stays disabled until then. */
async function pollConn(toolkit){
  if(typeof toolkit!=='string'||!toolkit||connBusy.has(toolkit))return;
  connBusy.add(toolkit);renderPolled();
  const res=await apiFetch(API_BASE+'/api/connections/'+encodeURIComponent(toolkit)+'/poll',{method:'POST'});
  connBusy.delete(toolkit);
  if(!res.ok)toast('poll failed: '+failText(res));
  else{
    const r=res.data||{};
    if(r.skipped)toast('poll skipped: '+String(r.skipped));
    else if(r.error)toast('poll failed: '+String(r.error));
    else toast((Number(r.new_events)||0)+' new from '+toolkit);
  }
  await loadConns();
}
/* One Refresh for the section: the polled apps and the connector cards. */
function refresh(){
  return Promise.all([loadConns(),connectorsView.refresh()]);
}
/* The connector cards keep their own listener (data-action); the section's
   controls carry data-act, so the two never answer each other's clicks. */
function onClick(e){
  const b=e.target.closest('button[data-act]');
  if(!b||b.disabled)return;
  switch(b.dataset.act){
    case'conns-refresh':refresh().catch(err=>console.error('Connections refresh failed:',err));break;
    case'conn-poll':pollConn(b.dataset.toolkit);break;
    case'conn-config':connectorsView.showPolling(b.dataset.toolkit);break;
  }
}

export default {
  id:'connections',label:'Connections',
  toolbar:connectorsView.toolbar,   /* the connector filter is the section's page search */
  async mount(el){
    root=el;
    el.innerHTML='<div class="conn-page">'
      +'<header class="conn-hero"><div>'
        +'<h2 id="setup-connections-title" tabindex="-1">Connections</h2>'
        +'<p>What the poller collects into the Inbox, and the apps and integrations behind it.</p>'
      +'</div><div class="conn-hero-actions">'
        +'<button class="conn-refresh" type="button" data-act="conns-refresh">Refresh</button>'
      +'</div></header>'
      +'<section class="conn-group" id="conn-polled-section" aria-labelledby="conn-polled-title">'
        +'<div class="conn-group-head"><div><h3 id="conn-polled-title">Polled apps</h3>'
          +'<p>Each app the poller watches, with its last poll.</p></div></div>'
        +'<div class="conn-polled" data-polled>'+polledHTML()+'</div>'
      +'</section>'
      +'<div class="conn-connectors-host"></div>'
    +'</div>';
    el.addEventListener('click',onClick);
    painted=paintKey();
    await connectorsView.mount(el.querySelector('.conn-connectors-host'));
  },
  show(){loadConns();setSlottedInterval('setup-conns-poll',loadConns,30000);connectorsView.show();},
  hide(){clearSlottedInterval('setup-conns-poll');},
  refresh,
};
