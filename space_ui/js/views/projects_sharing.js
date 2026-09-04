/* Project sharing inside the Files tab (List lens). Not a view: a helper the
   projects view imports, the way it imports core modules. Three surfaces:

     sharingStripHTML()     one line above the list — parked reason or
                            "running · last check", plus this workspace's id
                            with a copy button (the only place a user finds
                            the id to hand to a sharer)
     sharedWithYouHTML()    repos the relay reports as shared with this
                            workspace but not cloned here, each with a
                            copy-able clone command
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
import {setSlottedInterval} from '../core/store.js';
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
let onUpdate=()=>{};  /* projects view repaints its sharing surfaces */

export async function refreshSharingStatus(){
  const res=await apiFetch(API_BASE+'/api/project-sharing/status');
  statusRes=res;
  if(res.ok)status=res.data;
  return res;
}
/* 60 s matches the relay's own cadence; a faster UI poll would only re-read
   the same tick. Share/revoke refresh explicitly (see below). */
export function startSharingPoll(update){
  onUpdate=update||(()=>{});
  setSlottedInterval('projects-sharing',async()=>{await refreshSharingStatus();onUpdate();},60000);
}
/* after a write the relay is nudged and ticks within ~1 s; re-read shortly
   after so the strip and chips flip without waiting for the minute */
function refreshSoon(){setTimeout(async()=>{await refreshSharingStatus();onUpdate();},1500);}

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
  no_workspace_id:'no workspace id — set XO_PROJECT_ID in .env and restart',
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
    const n=Object.values(status.repos||{}).filter(r=>r.shared).length;
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
export function sharedWithYouHTML(){
  if(!status||status.cadence==='parked')return'';
  const avail=Object.entries(status.repos||{}).filter(([,r])=>r.available&&r.shared);
  if(!avail.length)return'';
  return'<div class="shr-inbox" id="prj-shared">'
    +'<div class="prj-ptitle">Shared with you · not on this machine yet</div>'
    +avail.map(([repo])=>'<div class="shr-inbox-row">'
      +'<span class="shr-repo"><span class="tchip st-available">shared</span><b>'+esc(repo)+'</b></span>'
      +'<span class="shr-hint">Clone it into your XO root ('+esc((status&&status.projects_root)||'~/xo-projects')+') and sync starts on its own within seconds.</span>'
      +'<span class="shr-cmd"><code>'+esc(cloneCmd(repo))+'</code>'
        +'<button class="shr-copy" type="button" data-copy="'+esc(cloneCmd(repo))+'" title="Copy clone command">copy</button></span>'
      +'</div>').join('')
    +'</div>';
}

/* copy buttons in the strip and inbox (and the panel's own) */
export function bindSharingCopies(root){
  root.querySelectorAll('.shr-copy[data-copy]').forEach(b=>b.addEventListener('click',async()=>{
    try{await navigator.clipboard.writeText(b.dataset.copy);toast('copied');}
    catch(e){toast('copy failed — select and copy by hand');}
  }));
}

/* ── drawer panel ─────────────────────────────────────────────────────────── */
function chip(projectId){
  const st=memberState(projectId);
  if(st==='live')return'<span class="tchip st-shared">shared</span>';
  if(st==='solo')return'<span class="tchip">not shared</span>';
  if(st==='disabled')return'<span class="tchip st-blocked">sharing parked</span>';
  return'<span class="tchip">waiting</span>';
}
function commitsHTML(d){
  const cs=d.commits||[];
  const behind=d.behind;
  const head='<div class="shr-sec-head"><span class="shr-sec-title">Commits on origin/'+esc(d.branch||'main')+'</span>'
    +(behind>0?'<span class="tchip st-shared">'+behind+' new · not applied</span>':'')+'</div>';
  if(!cs.length)return head+'<div class="prj-note">no commits on '+esc(d.source||'origin')+' yet</div>';
  return head+'<div class="prj-list">'+cs.slice(0,5).map(c=>'<div class="prj-li">'
    +'<code class="shr-hash">'+esc(short(c.hash))+'</code>'
    +'<span class="shr-subject">'+esc(c.subject)+'</span>'
    +'<span class="tmuted">'+esc(c.author)+' · '+rel(c.date)+'</span>'
    +'</div>').join('')+'</div>'
    +(behind>0?'<div class="prj-note">merge from your terminal — XO Space fetches, it never merges for you</div>':'');
}
function panelHTML(id,d){
  const st=memberState(id);
  return'<div class="shr-panel" data-project="'+esc(id)+'">'
    +'<div class="shr-sec">'+commitsHTML(d)+'</div>'
    +'<div class="shr-sec"><div class="shr-sec-head"><span class="shr-sec-title">Sharing</span>'+chip(id)+'</div>'
      +'<div class="shr-members" id="shr-members-'+esc(id)+'" data-state="'+st+'">'
        +'<div class="prj-note">'+(st==='live'?'loading…':IDLE_NOTE[st])+'</div></div>'
      +'<form class="shr-form" data-project="'+esc(id)+'">'
        +'<input class="tv-filter shr-input" name="ws" placeholder="recipient workspace id" '
          +'autocomplete="off" spellcheck="false" aria-label="Recipient workspace id">'
        +'<button class="sess-refresh" type="submit">Share</button>'
      +'</form>'
    +'</div></div>';
}
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
  const own=res.data.own_workspace_id,ms=res.data.members||[];
  const iOwn=ms.some(m=>m.role==='owner'&&m.workspace_id===own);
  still.innerHTML=ms.length?'<div class="shr-rows">'+ms.map(m=>'<div class="shr-row'+(m.status==='revoked'?' is-revoked':'')+'">'
      +'<code class="shr-id">'+esc(m.workspace_id)+'</code>'
      +'<span class="tchip">'+esc(m.role)+'</span>'
      +(m.status==='revoked'?'<span class="tchip st-blocked">revoked</span>':'')
      +(m.bound===false&&m.status==='active'?'<span class="shr-muted" title="that workspace has not checked in yet">not seen yet</span>':'')
      +(m.workspace_id===own?'<span class="shr-muted">(this workspace)</span>':'')
      +(iOwn&&m.role!=='owner'&&m.status==='active'
        ?'<button class="shr-revoke" type="button" data-id="'+esc(id)+'" data-ws="'+esc(m.workspace_id)+'">Revoke</button>':'')
      +'</div>').join('')+'</div>'
    :'<div class="prj-note">not shared with anyone yet</div>';
  still.querySelectorAll('.shr-revoke').forEach(b=>b.addEventListener('click',()=>revoke(b)));
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
async function revoke(btn){
  const id=btn.dataset.id,ws=btn.dataset.ws;
  btn.disabled=true;
  const res=await apiFetch(API_BASE+'/api/xo-projects/'+encodeURIComponent(id)+'/revoke',{method:'POST',body:{workspace_id:ws}});
  if(!res.ok){btn.disabled=false;toast('revoke failed: '+(res.offline?'xo-space is unreachable':res.error));return;}
  toast('revoked '+ws);
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
