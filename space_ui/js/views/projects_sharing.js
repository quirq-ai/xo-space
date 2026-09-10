/* Project sharing inside the Files tab. Not a view: a helper that the List
   lens (projects.js) and the Sharing lens (sharing.js) both import, the way
   they import core modules. Four surfaces:

     sharingStripHTML()     one line — parked reason or "running · last
                            check", plus this workspace's id with a copy
                            button (the only place a user finds the id to
                            hand to a sharer). Above the List; the Sharing
                            lens's header.
     sharedWithYouHTML()    repos the relay reports as shared with this
                            workspace but not cloned here, each with a
                            copy-able clone command
     sharedProjects()       the rows of the Sharing lens: repos cloned here
                            and shared with someone else
     sharingPanel           a drawer PANELS entry: recent commits + behind
                            count, members, share/revoke

   One source of truth: the last GET /api/project-sharing/status snapshot.
   "Is this shared" is always answered from it, never from /members, so the
   chip on the card and the member rows can never disagree. A missing entry
   is NOT proof of "not shared" — status is in-memory server-side and
   restarts empty — so absence and answer get different states:
     unknown   no snapshot yet, or the last check failed
     disabled  the loop is parked (no id / not signed in / switched off)
     solo      the relay checked and this repo is not shared
     live      shared: safe to fetch members and offer revoke
   Only `live` fetches /members; only `solo` says "not shared" as a fact. */
import {API_BASE,apiFetch} from '../core/api.js';
import {clearSlottedInterval,setSlottedInterval} from '../core/store.js';
import {toast} from '../core/ui.js';

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function rel(iso){
  if(!iso)return'—';
  const s=(Date.now()-new Date(iso).getTime())/1000;
  if(!isFinite(s))return'—';
  if(s<45)return'just now';
  if(s<3600)return Math.floor(s/60)+'m ago';
  if(s<86400)return Math.floor(s/3600)+'h ago';
  return Math.floor(s/86400)+'d ago';
}
const short=h=>String(h||'').slice(0,10);

/* ── status snapshot ──────────────────────────────────────────────────────── */
let status=null;      /* last good snapshot */
let statusRes=null;   /* last response, good or not, for the strip's wording */
/* Views that repaint their sharing surfaces after each read. Two lenses
   share one poll, so this is a set, not a single callback — a second
   subscriber must never silently replace the first. */
const subscribers=new Set();
function notify(){
  for(const fn of subscribers){
    try{fn();}catch(err){console.error('sharing subscriber failed:',err);}
  }
}
export function sharingStatus(){return status;}
export function sharingStatusRes(){return statusRes;}
export const sharingRel=rel;

export async function refreshSharingStatus(){
  const res=await apiFetch(API_BASE+'/api/project-sharing/status');
  statusRes=res;
  if(res.ok)status=res.data;
  return res;
}
/* 60 s matches the relay's own cadence; a faster UI poll would only re-read
   the same tick. Share/revoke refresh explicitly (see below). Slotted by
   name, so the second lens to mount joins the poll instead of doubling it. */
let polling=false;
export function startSharingPoll(update){
  if(typeof update==='function')subscribers.add(update);
  if(polling)return;
  polling=true;
  const tick=async()=>{await refreshSharingStatus();notify();syncFastPoll();};
  setSlottedInterval('projects-sharing',tick,60000);
  syncFastPoll();
}
/* While a clone is in flight the person is probably watching: re-read every
   3 s, and drop back to the minute as soon as nothing is cloning. Slotted, so
   it can never stack. */
let fastPolling=false;
function syncFastPoll(){
  const want=anyCloning();
  if(want&&!fastPolling){
    fastPolling=true;
    setSlottedInterval('projects-sharing-fast',async()=>{await refreshSharingStatus();notify();syncFastPoll();},3000);
  }else if(!want&&fastPolling){
    fastPolling=false;
    clearSlottedInterval('projects-sharing-fast');
  }
}
/* after a write the relay is nudged and ticks within ~1 s; re-read shortly
   after so the strip and chips flip without waiting for the minute */
function refreshSoon(){setTimeout(async()=>{await refreshSharingStatus();notify();},1500);}

function entryFor(projectId){
  if(!status||!status.repos)return null;
  for(const r of Object.values(status.repos))if(r.project===projectId)return r;
  return null;
}
export function memberState(projectId){
  if(!status)return'unknown';
  if(status.cadence==='parked')return'disabled';
  if(!status.last_poll_at||status.last_poll_ok===false)return'unknown';
  const e=entryFor(projectId);
  return e&&e.shared?'live':'solo';
}
const REASON={
  disabled:'sharing is switched off (PROJECT_SHARING_ENABLED=false)',
  no_workspace_id:'no workspace id — set XO_SPACE_ID in .env and restart',
  no_auth:'sign in to XO (or set XO_API_KEY) to share projects',
};
const IDLE_NOTE={
  disabled:'sharing is disabled for this workspace',
  unknown:'waiting for the relay to report',
  solo:'not shared yet',
};

/* ── strip ────────────────────────────────────────────────────────────────── */
export function sharingStripHTML(){
  let left;
  if(!statusRes)left='<span class="tchip">sharing</span><span class="shr-muted">checking…</span>';
  else if(!statusRes.ok)left='<span class="tchip st-blocked">sharing</span><span class="shr-muted">'
    +(statusRes.offline?'xo-space is unreachable':esc(statusRes.error))+'</span>';
  else if(status.cadence==='parked')left='<span class="tchip st-blocked">sharing parked</span>'
    +'<span class="shr-muted">'+esc(REASON[status.reason]||'parked')+'</span>';
  else{
    const ok=status.last_poll_ok;
    const n=Object.values(status.repos||{}).filter(r=>r.shared&&others(r)!==0).length;
    left='<span class="tchip'+(ok===false?' st-blocked':' st-shared')+'">sharing '+(ok===false?'check failed':'on')+'</span>'
      +'<span class="shr-muted">last check '+(status.last_poll_at?rel(status.last_poll_at):'pending')
      +(n?' · '+n+' shared repo'+(n===1?'':'s'):'')
      +' · watching '+esc(status.watch_branch||'main')+'</span>';
  }
  const ws=status&&status.own_workspace_id;
  const right=ws
    ?'<span class="shr-muted">this workspace</span><code class="shr-id">'+esc(ws)+'</code>'
      +'<button class="shr-copy" type="button" data-copy="'+esc(ws)+'" title="Copy workspace id">copy</button>'
    :'';
  return'<div class="shr-strip" id="prj-sharing-strip"><span class="shr-left">'+left+'</span>'
    +'<span class="prj-spacer"></span><span class="shr-right">'+right+'</span></div>';
}

/* ── shared with you (not cloned here) ────────────────────────────────────── */
/* The relay only sees clones directly under the XO root, so the command
   must target that exact directory; the server reports it in the status. */
function cloneCmd(repo){
  const name=repo.split('/').pop();
  const root=(status&&status.projects_root)||'~/xo-projects';
  return'git clone https://'+repo+'.git '+root.replace(/\/$/,'')+'/'+name;
}
/* One row per repo shared with this workspace and not cloned here. The relay
   clones these by itself; the row says where that stands and, when it cannot
   proceed, why and what to do. The manual command stays as the fallback. */
function inboxRow(repo,r){
  const c=r.clone||null,st=c?c.state:null;
  const cmd='<span class="shr-cmd"><code>'+esc(cloneCmd(repo))+'</code>'
    +'<button class="shr-copy" type="button" data-copy="'+esc(cloneCmd(repo))+'" title="Copy clone command">copy</button></span>';
  let chip,hint,extra='';
  if(st==='cloning'){
    chip='<span class="tchip st-shared">cloning…</span>';
    hint='XO Space is cloning this into your XO root. It appears in the list below when done.';
  }else if(st==='needs_auth'){
    chip='<span class="tchip st-blocked">needs GitHub sign-in</span>';
    hint='This looks like a private repo. Connect GitHub in Setup and XO Space will clone it on the next check, or clone it yourself:';
    extra='<button class="sess-refresh shr-connect" type="button" data-connect-github>Connect GitHub</button>';
  }else if(st==='no_access'){
    chip='<span class="tchip st-blocked">no access</span>';
    hint=esc(c.detail||'the connected GitHub account cannot see this repo')
      +'. Ask the repo owner to add that account as a collaborator; XO Space retries on its own. Or clone it yourself with an account that has access:';
  }else if(st==='exists'){
    chip='<span class="tchip st-blocked">folder in the way</span>';
    hint=esc(c.detail||'a folder with this name already exists here')+'. Move or rename it and XO Space will try again, or clone under another name:';
  }else if(st==='error'){
    chip='<span class="tchip st-blocked">clone failed</span>';
    hint=esc(c.detail||'git clone failed')+' — XO Space will retry later, or clone it yourself:';
  }else{
    chip='<span class="tchip st-available">shared</span>';
    hint='Shared with you. XO Space clones it into your XO root ('+esc((status&&status.projects_root)||'~/xo-projects')+') on its next check, or clone it yourself:';
  }
  return'<div class="shr-inbox-row" data-clone-state="'+esc(st||'')+'">'
    +'<span class="shr-repo">'+chip+'<b>'+esc(repo)+'</b></span>'
    +'<span class="shr-hint">'+hint+'</span>'
    +(extra?'<span class="shr-actions">'+extra+'</span>':'')
    +(st==='cloning'?'':cmd)
    +'</div>';
}
export function sharedWithYouHTML(){
  if(!status||status.cadence==='parked')return'';
  const avail=Object.entries(status.repos||{}).filter(([,r])=>r.available&&r.shared);
  if(!avail.length)return'';
  return'<div class="shr-inbox" id="prj-shared">'
    +'<div class="prj-ptitle">Shared with you · not on this machine yet</div>'
    +avail.map(([repo,r])=>inboxRow(repo,r)).join('')
    +'</div>';
}
/* True while any shared repo is mid-clone: the tab polls faster then. */
export function anyCloning(){
  return!!status&&Object.values(status.repos||{}).some(r=>r.clone&&r.clone.state==='cloning');
}
/* True once per new `cloned` transition, so the projects view can reload
   its list and show the new folder without waiting for a manual refresh. */
let seenCloned=0;
export function consumeNewClone(){
  if(!status)return false;
  const n=(status.recent||[]).filter(e=>e.kind==='cloned').length;
  const fresh=n>seenCloned;
  seenCloned=n;
  return fresh;
}
/* Cross-view jump for the Connect GitHub button (views never import each
   other; the projects view hands us ctx.switchTo on mount). */
let navTo=null;
export function setSharingNav(fn){navTo=fn;}
export function bindSharingActions(root){
  root.querySelectorAll('[data-connect-github]').forEach(b=>b.addEventListener('click',()=>{
    if(navTo)navTo('secrets');else toast('open Setup and connect GitHub');
  }));
}

/* copy buttons in the strip and inbox (and the panel's own) */
export function bindSharingCopies(root){
  root.querySelectorAll('.shr-copy[data-copy]').forEach(b=>b.addEventListener('click',async()=>{
    try{await navigator.clipboard.writeText(b.dataset.copy);toast('copied');}
    catch(e){toast('copy failed — select and copy by hand');}
  }));
}

/* ── row chip ─────────────────────────────────────────────────────────────
   Shown beside the project name in the list for every repo the relay knows
   is shared, so "which of my projects are synced" is visible without opening
   a drawer. For the first day after XO Space cloned it, say so. */
const DAY=86400*1000;
/* How many OTHER workspaces can see the repo: the swarm's active-row count
   minus this one. The owner row never goes away, so a repo whose last member
   was revoked still comes back as a member with a count of 1 — that is "not
   shared" to a person. null when the swarm did not report a count (older
   server), in which case "shared" is still the honest answer. */
function others(e){
  const m=e&&e.members;
  return(typeof m==='number')?Math.max(0,m-1):null;
}
/* The Sharing lens's rows: every repo cloned here (it has a project) that
   someone else can see. Same rule as the row chip, so the lens and the List
   never disagree about what counts as shared. Per-repo timestamps and the
   last fetch error ride along from the snapshot; the behind count does not
   live there — the lens asks /commits per row. */
export function sharedProjects(){
  if(!status)return[];
  return Object.entries(status.repos||{})
    .filter(([,r])=>r.shared&&r.project&&others(r)!==0)
    .map(([repo,r])=>({repo,project:r.project,others:others(r),
      lastFetchAt:r.last_fetch_at||null,lastError:r.last_error||null,
      autoClonedAt:r.auto_cloned_at||null}));
}
/* How many repos sit in the "shared with you" inbox (for the lens eyebrow). */
export function sharedWithYouCount(){
  if(!status||status.cadence==='parked')return 0;
  return Object.values(status.repos||{}).filter(r=>r.available&&r.shared).length;
}
export function sharingRowChip(projectId){
  const e=entryFor(projectId);
  if(!e||!e.shared)return'';
  const n=others(e);
  if(n===0)return'';
  const at=e.auto_cloned_at;
  const fresh=at&&(Date.now()-new Date(at).getTime())<DAY;
  return'<span class="tchip st-shared prj-shr-chip" title="synced through project sharing">shared'
    +(n?' with '+n:'')+(fresh?' · auto-cloned '+rel(at):'')+'</span>';
}
function clonedNote(projectId){
  const e=entryFor(projectId);
  if(!e||!e.auto_cloned_at)return'';
  const d=new Date(e.auto_cloned_at);
  return'<span class="shr-muted" title="'+esc(e.auto_cloned_at)+'">cloned by XO Space on '
    +esc(d.toLocaleDateString(undefined,{dateStyle:'medium'}))+'</span>';
}

/* ── drawer panel ─────────────────────────────────────────────────────────── */
function chip(projectId){
  const st=memberState(projectId);
  if(st==='live'){
    const n=others(entryFor(projectId));
    if(n===0)return'<span class="tchip" title="you own this; nobody else can see it">not shared</span>';
    return'<span class="tchip st-shared">shared'+(n?' with '+n:'')+'</span>';
  }
  if(st==='solo')return'<span class="tchip">not shared</span>';
  if(st==='disabled')return'<span class="tchip st-blocked">sharing parked</span>';
  return'<span class="tchip">waiting</span>';
}
/* The list is origin/<branch> newest-first and `behind` counts the commits
   HEAD does not have, so the top `behind` rows are exactly the unapplied
   ones (fast-forward case). Highlight them and offer the exact merge
   command; XO Space fetches, it never merges for you. */
function commitsHTML(d){
  const cs=d.commits||[];
  const behind=d.behind|0;
  const branch=d.branch||'main';
  const head='<div class="shr-sec-head"><span class="shr-sec-title">Commits on origin/'+esc(branch)+'</span>'
    +(behind>0
      ?'<span class="tchip st-shared">'+behind+' new · not applied</span>'
      :(d.behind===0?'<span class="tchip">up to date</span>':''))+'</div>';
  if(!cs.length)return head+'<div class="prj-note">no commits on '+esc(d.source||'origin')+' yet</div>';
  const apply=d.path?'git -C '+d.path+' merge --ff-only origin/'+branch:'';
  return head+'<div class="prj-list">'+cs.slice(0,5).map((c,i)=>'<div class="prj-li'+(i<behind?' is-new':'')+'">'
    +'<code class="shr-hash">'+esc(short(c.hash))+'</code>'
    +'<span class="shr-subject">'+esc(c.subject)+'</span>'
    +(i<behind?'<span class="tchip st-shared shr-newtag">new</span>':'')
    +'<span class="tmuted">'+esc(c.author)+' · '+rel(c.date)+'</span>'
    +'</div>').join('')+'</div>'
    +(behind>0&&apply
      ?'<div class="shr-apply"><span class="shr-muted">apply</span><code>'+esc(apply)+'</code>'
        +'<button class="shr-copy" type="button" data-copy="'+esc(apply)+'" title="Copy merge command">copy</button></div>'
      :'');
}
function panelHTML(id,d){
  const st=memberState(id);
  return'<div class="shr-panel" data-project="'+esc(id)+'">'
    +'<div class="shr-sec">'+commitsHTML(d)+'</div>'
    +'<div class="shr-sec"><div class="shr-sec-head"><span class="shr-sec-title">Members</span>'+chip(id)+clonedNote(id)+'</div>'
      +'<div class="shr-members" id="shr-members-'+esc(id)+'" data-state="'+st+'">'
        +(st==='live'?loadingHTML():'<div class="prj-note">'+IDLE_NOTE[st]+'</div>')+'</div>'
      +'<form class="shr-form" data-project="'+esc(id)+'">'
        +'<input class="tv-filter shr-input" name="ws" placeholder="recipient workspace id" '
          +'autocomplete="off" spellcheck="false" aria-label="Recipient workspace id">'
        +'<button class="sess-refresh" type="submit">Share</button>'
      +'</form>'
    +'</div></div>';
}
/* Loading is a shape, not a word — the same skeleton lines the other drawer
   panels show while their fetch is in flight. */
const loadingHTML=()=>'<div class="prj-skel is-sm"></div><div class="prj-skel is-sm is-short"></div>';
/* A workspace id is long and opaque; rows only need enough of it to tell
   members apart. The full id is the hover title and the copy payload. */
function shortId(id){
  const s=String(id||'');
  return s.length<=14?s:s.slice(0,7)+'…'+s.slice(-4);
}
/* owner first, then active members, then the revoked history */
const memberRank=m=>m.role==='owner'?0:m.status==='revoked'?2:1;
function memberRow(m,own,iOwn,id){
  const canRevoke=iOwn&&m.role!=='owner'&&m.status==='active';
  return'<div class="shr-row'+(m.status==='revoked'?' is-revoked':'')+'" data-ws="'+esc(m.workspace_id)+'">'
    +'<code class="shr-id" title="'+esc(m.workspace_id)+'">'+esc(shortId(m.workspace_id))+'</code>'
    +'<button class="shr-copy" type="button" data-copy="'+esc(m.workspace_id)+'" title="Copy workspace id">copy</button>'
    +'<span class="tchip">'+esc(m.role)+'</span>'
    +(m.workspace_id===own?'<span class="tchip st-shared">you</span>':'')
    +(m.status==='revoked'?'<span class="tchip st-blocked">revoked</span>':'')
    +(m.bound===false&&m.status==='active'?'<span class="shr-muted" title="that workspace has not checked in yet">not seen yet</span>':'')
    +(canRevoke?'<span class="shr-act">'+revokeButtonHTML(id,m.workspace_id)+'</span>':'')
    +'</div>';
}
const revokeButtonHTML=(id,ws)=>'<button class="shr-revoke" type="button" data-id="'+esc(id)+'" data-ws="'+esc(ws)+'">Revoke</button>';
/* Revoke asks first, in the row: the button becomes a question with Confirm
   and Cancel. No browser dialog, nothing is sent until Confirm. */
const revokeConfirmHTML=(id,ws)=>'<span class="shr-confirm" role="group" aria-label="Confirm revoke">'
  +'<span class="shr-muted">revoke '+esc(shortId(ws))+'?</span>'
  +'<button class="shr-confirm-yes" type="button" data-id="'+esc(id)+'" data-ws="'+esc(ws)+'">Confirm</button>'
  +'<button class="shr-confirm-no" type="button" data-id="'+esc(id)+'" data-ws="'+esc(ws)+'">Cancel</button></span>';
const emptyMembersHTML=()=>'<div class="shr-empty"><b>Not shared with anyone yet</b>'
  +'<p>Enter another workspace’s id below. They find theirs in the strip at the top of the Sharing lens.</p></div>';
async function fillMembers(id){
  const box=document.getElementById('shr-members-'+id);
  if(!box)return;
  const res=await apiFetch(API_BASE+'/api/xo-projects/'+encodeURIComponent(id)+'/members');
  const still=document.getElementById('shr-members-'+id);
  if(!still)return;
  if(!res.ok){
    /* not a member yet / no origin / swarm down: the share form below still
       works (the first share creates the group), so say why and move on */
    still.innerHTML='<div class="prj-note">'+(res.offline?'xo-space is unreachable':esc(res.error))+'</div>';
    return;
  }
  const own=res.data.own_workspace_id,ms=(res.data.members||[]).slice();
  const iOwn=ms.some(m=>m.role==='owner'&&m.workspace_id===own);
  /* "shared" means someone other than the owner can see it. A group that
     holds only you (or only revoked rows) is the empty state, with the
     revoked history kept underneath so a revoke does not vanish. */
  const others=ms.filter(m=>m.status==='active'&&!(m.role==='owner'&&m.workspace_id===own));
  const shown=others.length?ms:ms.filter(m=>m.status==='revoked');
  shown.sort((a,b)=>memberRank(a)-memberRank(b));
  still.innerHTML=(others.length?'':emptyMembersHTML())
    +(shown.length?'<div class="shr-rows">'+shown.map(m=>memberRow(m,own,iOwn,id)).join('')+'</div>':'');
  bindSharingCopies(still);
  /* one delegated listener per box: the box outlives its rows, which are
     rebuilt on every refresh */
  if(!still.dataset.bound){
    still.dataset.bound='1';
    still.addEventListener('click',onMembersClick);
  }
}
function onMembersClick(e){
  const b=e.target.closest('button');
  if(!b)return;
  const slot=b.closest('.shr-act');
  if(b.classList.contains('shr-revoke')&&slot){
    slot.innerHTML=revokeConfirmHTML(b.dataset.id,b.dataset.ws);
    slot.querySelector('.shr-confirm-yes').focus();
  }else if(b.classList.contains('shr-confirm-no')&&slot){
    slot.innerHTML=revokeButtonHTML(b.dataset.id,b.dataset.ws);
    slot.querySelector('.shr-revoke').focus();
  }else if(b.classList.contains('shr-confirm-yes')&&slot){
    revoke(b.dataset.id,b.dataset.ws,slot);
  }
}
async function share(form){
  const id=form.dataset.project;
  const input=form.querySelector('input[name=ws]');
  const btn=form.querySelector('button');
  const ws=(input.value||'').trim();
  if(!ws){toast('enter the recipient’s workspace id');input.focus();return;}
  btn.disabled=true;
  const res=await apiFetch(API_BASE+'/api/xo-projects/'+encodeURIComponent(id)+'/share',{method:'POST',body:{workspace_id:ws}});
  btn.disabled=false;
  if(!res.ok){toast('share failed: '+(res.offline?'xo-space is unreachable':res.error));return;}
  toast('shared with '+ws);
  input.value='';
  /* the swarm has it; our relay learns on the nudged tick. Say so rather
     than fetching member rows the chip would contradict. */
  const box=document.getElementById('shr-members-'+id);
  if(box&&box.dataset.state!=='live')
    box.innerHTML='<div class="prj-note">shared with '+esc(ws)+' · appears here after the next check</div>';
  else fillMembers(id);
  refreshSoon();
}
async function revoke(id,ws,slot){
  slot.querySelectorAll('button').forEach(b=>{b.disabled=true;});
  const res=await apiFetch(API_BASE+'/api/xo-projects/'+encodeURIComponent(id)+'/revoke',{method:'POST',body:{workspace_id:ws}});
  if(!res.ok){
    /* back to the plain button; the toast carries the reason */
    if(slot.isConnected)slot.innerHTML=revokeButtonHTML(id,ws);
    toast('revoke failed: '+(res.offline?'xo-space is unreachable':res.error));
    return;
  }
  toast('revoked '+shortId(ws));
  fillMembers(id);
  refreshSoon();
}

export const sharingPanel={
  key:'sharing',title:'Sharing',
  path:id=>'/api/xo-projects/'+encodeURIComponent(id)+'/commits?limit=5',
  render:d=>panelHTML(d.project_id,d),
  /* the panel has buttons; projects.js calls bind() after render() */
  bind(el,id){
    if(memberState(id)==='live')fillMembers(id);
    const f=el.querySelector('.shr-form');
    if(f)f.addEventListener('submit',e=>{e.preventDefault();share(f);});
    bindSharingCopies(el);
  },
};

/* A status change can flip a project from solo to live (or back) without a
   drawer re-render; called by the projects view on each refresh. */
export function syncSharingPanel(id){
  const box=document.getElementById('shr-members-'+id);
  if(!box)return;
  const st=memberState(id);
  if(box.dataset.state===st)return;
  box.dataset.state=st;
  const chipEl=box.parentElement&&box.parentElement.querySelector('.shr-sec-head .tchip');
  if(chipEl)chipEl.outerHTML=chip(id);
  if(st==='live')fillMembers(id);
  else box.innerHTML='<div class="prj-note">'+IDLE_NOTE[st]+'</div>';
}
