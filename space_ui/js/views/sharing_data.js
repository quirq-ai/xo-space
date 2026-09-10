/* Project sharing — the data half of the Sharing pane (views/sharing.js is
   the paint half). Not a view: one status poll, the BFF calls, and the small
   shared vocabulary (escape, relative time, short ids, the clone and invite
   text). Nothing here touches the DOM.

   One source of truth: the last GET /api/project-sharing/status snapshot.
   "Is this shared" is always answered from it, never from /members, so a
   card's chip and its member rows can never disagree. A missing entry is
   NOT proof of "not shared" — status is in-memory server-side and restarts
   empty — so absence and answer get different states:
     unknown   no snapshot yet, or the last check failed
     disabled  the loop is parked (no id / not signed in / switched off)
     solo      the relay checked and this repo is not shared
     live      shared: safe to fetch members and offer revoke
   Only `live` fetches /members; only `solo` says "not shared" as a fact. */
import {API_BASE,apiFetch} from '../core/api.js';
import {clearSlottedInterval,setSlottedInterval} from '../core/store.js';

export const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
export function rel(iso){
  if(!iso)return'—';
  const s=(Date.now()-new Date(iso).getTime())/1000;
  if(!isFinite(s))return'—';
  if(s<45)return'just now';
  if(s<3600)return Math.floor(s/60)+'m ago';
  if(s<86400)return Math.floor(s/3600)+'h ago';
  return Math.floor(s/86400)+'d ago';
}
export const shortHash=h=>String(h||'').slice(0,10);
/* A workspace id is long and opaque; rows only need enough of it to tell
   members apart. The full id is the hover title and the copy payload. */
export function shortId(id){
  const s=String(id||'');
  return s.length<=14?s:s.slice(0,7)+'…'+s.slice(-4);
}

/* ── status snapshot ──────────────────────────────────────────────────── */
let status=null;      /* last good snapshot */
let statusRes=null;   /* last response, good or not, for the strip's wording */
export function sharingStatus(){return status;}
export function sharingStatusRes(){return statusRes;}

/* Whoever paints from the snapshot subscribes; a set, so a second
   subscriber can never silently replace the first. */
const subscribers=new Set();
function notify(){
  for(const fn of subscribers){
    try{fn();}catch(err){console.error('sharing subscriber failed:',err);}
  }
}
export async function refreshSharingStatus(){
  const res=await apiFetch(API_BASE+'/api/project-sharing/status');
  statusRes=res;
  if(res.ok)status=res.data;
  return res;
}
/* 60 s matches the relay's own cadence; a faster UI poll would only re-read
   the same tick. Writes refresh explicitly (refreshSoon). Slotted by name,
   so mounting twice joins the poll instead of doubling it. */
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
   3 s, and drop back to the minute as soon as nothing is cloning. */
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
   after so chips flip without waiting for the minute */
export function refreshSoon(ms=1500){
  return new Promise(done=>setTimeout(async()=>{await refreshSharingStatus();notify();done();},ms));
}
export function anyCloning(){
  return!!status&&Object.values(status.repos||{}).some(r=>r.clone&&r.clone.state==='cloning');
}
/* True once per new `cloned` transition, so the pane can reload the project
   catalog and name the new folder without a manual refresh. */
let seenCloned=0;
export function consumeNewClone(){
  if(!status)return false;
  const n=(status.recent||[]).filter(e=>e.kind==='cloned').length;
  const fresh=n>seenCloned;
  seenCloned=n;
  return fresh;
}

export const REASON={
  disabled:'sharing is switched off (PROJECT_SHARING_ENABLED=false)',
  no_workspace_id:'no workspace id — set XO_SPACE_ID in .env and restart',
  no_auth:'sign in to XO (or set XO_API_KEY) to share projects',
};
export const parked=()=>!!status&&status.cadence==='parked';

/* How many OTHER workspaces can see the repo: the swarm's active-row count
   minus this one. The owner row never goes away, so a repo whose last member
   was revoked still comes back as a member with a count of 1 — that is "not
   shared" to a person. null when the swarm did not report a count (older
   server), in which case "shared" is still the honest answer. */
export function others(e){
  const m=e&&e.members;
  return(typeof m==='number')?Math.max(0,m-1):null;
}
export function memberState(projectId){
  if(!status)return'unknown';
  if(status.cadence==='parked')return'disabled';
  if(!status.last_poll_at||status.last_poll_ok===false)return'unknown';
  const e=entryFor(projectId);
  return e&&e.shared?'live':'solo';
}
export function entryFor(projectId){
  if(!status||!status.repos)return null;
  for(const r of Object.values(status.repos))if(r.project===projectId)return r;
  return null;
}
/* Every repo the relay knows, normalised for the pane:
     mine      cloned here (has a project id) and known to the swarm
     incoming  shared with this workspace, not cloned here yet
   Per-repo timestamps and the last fetch error ride along; the behind count
   does not live in the snapshot — the pane asks /commits per project. */
export function repos(){
  if(!status)return[];
  return Object.entries(status.repos||{}).map(([repo,r])=>({
    repo,project:r.project||null,shared:!!r.shared,
    mine:!!r.project&&!!r.shared,
    incoming:!r.project&&!!r.available&&!!r.shared,
    others:others(r),
    lastFetchAt:r.last_fetch_at||null,lastError:r.last_error||null,
    clone:r.clone||null,autoClonedAt:r.auto_cloned_at||null,
  })).filter(r=>r.mine||r.incoming);
}

/* ── copy payloads ───────────────────────────────────────────────────── */
/* The relay only sees clones directly under the XO root, so the command
   must target that exact directory; the server reports it in the status. */
export function cloneCmd(repo){
  const name=repo.split('/').pop();
  const root=(status&&status.projects_root)||'~/xo-projects';
  return'git clone https://'+repo+'.git '+root.replace(/\/$/,'')+'/'+name;
}
export function applyCmd(path,branch){
  return'git -C '+path+' merge --ff-only origin/'+(branch||'main');
}
/* One paste instead of a back-and-forth: everything the other side needs
   to share with this workspace, including where the button is. */
export function inviteText(){
  const ws=status&&status.own_workspace_id;
  if(!ws)return'';
  return'Share your project with me on XO Space: open Files → Sharing → “+ Share a project”, '
    +'pick the repo and paste my workspace id: '+ws;
}

/* ── BFF calls (the browser never talks to the swarm) ─────────────────── */
const P=id=>API_BASE+'/api/xo-projects/'+encodeURIComponent(id);
export const fetchCatalog=()=>apiFetch(API_BASE+'/api/xo-projects');
export const fetchCommits=(id,limit=5)=>apiFetch(P(id)+'/commits?limit='+limit);
export const fetchMembers=id=>apiFetch(P(id)+'/members');
export const share=(id,ws)=>apiFetch(P(id)+'/share',{method:'POST',body:{workspace_id:ws}});
export const revoke=(id,ws)=>apiFetch(P(id)+'/revoke',{method:'POST',body:{workspace_id:ws}});
export const apply=id=>apiFetch(P(id)+'/apply',{method:'POST',body:{}});
export const checkNow=()=>apiFetch(API_BASE+'/api/project-sharing/check',{method:'POST',body:{}});
/* one wording for a failed call, everywhere */
export const failText=res=>res.notImplemented?'not available for the active agent'
  :res.offline?'xo-space is unreachable':String(res.error||'request failed');
