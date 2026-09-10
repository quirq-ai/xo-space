/* Sharing — the fourth Files lens, and the whole of project sharing in the
   Space UI (issue #83). Designed around the loop, not a layout: share once,
   then commits flow and each side applies.

     Rail         the "shared with you" inbox (clone state, and the one
                  thing to click when only a person can move it on), then
                  every project shared from this machine, work waiting
                  first, each with a single sync state (N new / in sync /
                  fetch failed) and an Apply button when it is behind.
     Detail       the selected project: commits on origin/<branch> with
                  Apply (fast-forward) and the by-hand command, members
                  (owner first, short ids, inline revoke confirm), share.
     Composer     "+ Share a project" swaps into the detail panel: pick a
                  project, paste the recipient's workspace id. "Copy
                  invite" turns the id exchange into one paste. "Check
                  now" nudges the relay instead of waiting out the minute.

   Data comes through views/sharing_data.js (one status poll, the BFF
   calls); this file only paints and handles events. One delegated click /
   submit / input listener on the section: the pane re-renders from state,
   so nothing is bound per element. */
import {toast} from '../core/ui.js';
import {esc,rel,shortId,shortHash,sharingStatus,sharingStatusRes,refreshSharingStatus,
  startSharingPoll,refreshSoon,consumeNewClone,REASON,parked,memberState,entryFor,repos,
  cloneCmd,applyCmd,inviteText,fetchCatalog,fetchCommits,fetchMembers,share,revoke,apply,
  checkNow,failText} from './sharing_data.js?v=20260910-sharingpane1';

const plural=(n,word)=>n.toLocaleString()+' '+word+(n===1?'':'s');
const dtfmt=iso=>iso?new Date(iso).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}):'—';

let root=null;
let go=()=>{};            /* ctx.switchTo, captured on mount */
let names=new Map();      /* project id -> display name, from the catalog */
let catalog=[];           /* every project, for the composer's picker */
let commits=new Map();    /* project id -> {ok,behind,branch,path,commits,error} */
let members=new Map();    /* project id -> {ok,own,members,error} */
let open=null;            /* the expanded card's project id */
let composer=null;        /* null | {pick,filter,ws} */
let busy=new Set();       /* project ids with a write in flight */
let confirmRevoke=null;   /* {id,ws} while a revoke waits for Confirm */
let renderedAt=0;

export default {
  /* No tab of its own: the Files tab owns the nav slot and this is its
     fourth lens, reached from the List | Graph | Tree | Sharing pill (or
     #/sharing). */
  id:'sharing',label:'Sharing',order:4,nav:false,parent:'projects',
  async mount(el,ctx){
    root=el;
    go=ctx.switchTo;
    el.innerHTML='<div class="prj shl">'+skeleton()+'</div>';
    root.addEventListener('click',onClick);
    root.addEventListener('submit',onSubmit);
    root.addEventListener('input',onInput);
    startSharingPoll(onStatus);
    await Promise.all([loadCatalog(),refreshSharingStatus()]);
    render();
    loadCommits();
  },
  /* Coming back to the lens re-reads; right after mount the paint is fresh
     and a second read would only repeat it. */
  show(){if(root&&Date.now()-renderedAt>2000)refresh();}
};

const skeleton=()=>'<div class="prj-head"></div><div class="prj-rows">'
  +'<div class="prj-skel"></div>'.repeat(3)+'</div>';

/* ── loads ────────────────────────────────────────────────────────────── */
async function loadCatalog(){
  const res=await fetchCatalog();
  if(!res.ok)return; /* cards fall back to the id; the pane still works */
  catalog=res.data.items||[];
  names=new Map(catalog.map(p=>[p.id,p.display_name||p.id]));
}
/* One small request per project cloned here, no barrier: the behind count
   and branch are not in the status snapshot. Re-run on every poll tick so
   "N new" tracks the relay's fetches. */
let commitsReady=false;   /* the first behind counts are in: safe to pick a default row */
async function loadCommits(){
  const mine=repos().filter(r=>r.mine);
  await Promise.all(mine.map(async r=>{
    const res=await fetchCommits(r.project,5);
    commits.set(r.project,res.ok
      ?{ok:true,behind:res.data.behind,branch:res.data.branch,path:res.data.path,commits:res.data.commits||[]}
      :{ok:false,error:failText(res)});
  }));
  commitsReady=true;
  if(!editing())render();
}
async function loadMembers(id){
  const res=await fetchMembers(id);
  members.set(id,res.ok
    ?{ok:true,own:res.data.own_workspace_id,members:res.data.members||[]}
    :{ok:false,error:failText(res)});
  paintMembers(id);
}
async function refresh(){
  await Promise.all([loadCatalog(),refreshSharingStatus()]);
  render();
  loadCommits();
}
/* A poll tick repaints everything unless the person is mid-edit (composer
   open, a revoke waiting for Confirm): then only the strip and the eyebrow
   move, so a keystroke or a pending question is never wiped. */
function onStatus(){
  if(!root)return;
  if(consumeNewClone())loadCatalog().then(()=>{if(!editing())render();});
  if(editing()){
    const strip=root.querySelector('#prj-sharing-strip');
    if(strip)strip.outerHTML=stripHTML();
    const count=root.querySelector('#shl-count');
    if(count)count.textContent=summary(model());
  }else render();
  loadCommits();
}
const editing=()=>!!composer||!!confirmRevoke;
/* the open card's detail: commits are already loading; members only when
   the relay says the repo is live */
function ensureDetail(id){
  if(memberState(id)==='live'&&!members.has(id))loadMembers(id);
}

/* ── model ────────────────────────────────────────────────────────────── */
/* One row per repo, with a single sync state and, when a person has to act,
   what that action is: apply | auth | manual | cloning. */
function model(){
  const rows=repos().map(r=>{
    const c=r.mine?commits.get(r.project):null;
    const behind=c&&c.ok&&typeof c.behind==='number'?c.behind:null;
    const name=r.mine?(names.get(r.project)||r.project):r.repo.split('/').pop();
    let state='pending',need=null;
    if(r.incoming){
      const st=r.clone&&r.clone.state;
      state=st||'available';
      if(st==='needs_auth')need='auth';
      else if(st==='no_access'||st==='exists'||st==='error')need='manual';
      else if(st==='cloning')need='cloning';
    }else if(behind>0){state='behind';need='apply';}
    else if(c&&!c.ok)state='error';
    else if(r.lastError)state='fetchfail';
    else if(behind===0)state='sync';
    return{...r,name,behind,c,state,need,key:r.mine?r.project:r.repo};
  });
  const urgency=r=>r.need==='apply'||r.need==='auth'||r.need==='manual'?2:r.need?1:0;
  rows.sort((a,b)=>urgency(b)-urgency(a)||(b.behind||0)-(a.behind||0)||a.name.localeCompare(b.name));
  return rows;
}
const actionable=m=>m.filter(r=>r.need&&r.need!=='cloning');

/* ── paint ────────────────────────────────────────────────────────────── */
function render(){
  if(!root)return;
  const m=model();
  root.querySelector('.prj').innerHTML=headHTML(m)+stripHTML()+bodyHTML(m);
  renderedAt=Date.now();
  if(open&&!composer)ensureDetail(open);
}
function summary(m){
  const res=sharingStatusRes();
  if(!res)return'checking…';
  if(!res.ok)return'sharing status unavailable';
  if(parked())return'sharing parked';
  const mine=m.filter(r=>r.mine).length,inc=m.filter(r=>r.incoming).length,need=actionable(m).length;
  if(!m.length)return'nothing shared yet';
  return mine+' shared'+(inc?' · '+inc+' incoming':'')+(need?' · '+need+' need you':' · all in sync');
}
function headHTML(m){
  const off=parked()||!sharingStatusRes()||!sharingStatusRes().ok;
  return'<div class="prj-head">'
    +'<span class="prj-eyebrow" id="shl-count">'+esc(summary(m))+'</span>'
    +'<span class="prj-spacer"></span>'
    +(composer
      ?'<button class="sess-refresh" type="button" data-act="composer">Cancel</button>'
      :'<button class="sess-refresh shl-primary" type="button" data-act="composer"'+(off?' disabled':'')+'>+ Share a project</button>')
    +'<button class="sess-refresh" type="button" data-act="check" title="Ask the relay to check now instead of waiting for the next minute"'+(off?' disabled':'')+'>Check now</button>'
  +'</div>';
}
function stripHTML(){
  const status=sharingStatus(),res=sharingStatusRes();
  let left;
  if(!res)left='<span class="tchip">sharing</span><span class="shr-muted">checking…</span>';
  else if(!res.ok)left='<span class="tchip st-blocked">sharing</span><span class="shr-muted">'+esc(failText(res))+'</span>';
  else if(status.cadence==='parked')left='<span class="tchip st-blocked">sharing parked</span>'
    +'<span class="shr-muted">'+esc(REASON[status.reason]||'parked')+'</span>';
  else{
    const ok=status.last_poll_ok;
    left='<span class="tchip'+(ok===false?' st-blocked':' st-shared')+'">sharing '+(ok===false?'check failed':'on')+'</span>'
      +'<span class="shr-muted">last check '+(status.last_poll_at?esc(rel(status.last_poll_at)):'pending')
      +' · every minute · watching '+esc(status.watch_branch||'main')+'</span>';
  }
  const ws=status&&status.own_workspace_id;
  const right=ws
    ?'<span class="shr-muted">your id</span><code class="shr-id" title="'+esc(ws)+'">'+esc(ws)+'</code>'
      +'<button class="shr-copy" type="button" data-act="copy" data-copy="'+esc(ws)+'" title="Copy workspace id">copy</button>'
      +'<button class="shr-copy is-accent" type="button" data-act="copy" data-copy="'+esc(inviteText())+'" '
        +'title="Copy a one-line invite: what to click and your id">copy invite</button>'
    :'';
  return'<div class="shr-strip" id="prj-sharing-strip"><span class="shr-left">'+left+'</span>'
    +'<span class="prj-spacer"></span><span class="shr-right">'+right+'</span></div>';
}

/* ── rail: inbox + shared projects ───────────────────────────────────── */
/* An incoming repo's row says where the clone stands and, when only a
   person can move it on, offers the one thing to click. */
function inboxRow(r){
  let what='',why='',acts='';
  if(r.need==='auth'){
    what='private repo · needs GitHub';
    why='connect GitHub once; XO Space clones it on the next check';
    acts='<button class="sess-refresh is-sm" type="button" data-act="connect">Connect GitHub</button>'
      +'<button class="shr-copy" type="button" data-act="copy" data-copy="'+esc(cloneCmd(r.repo))+'" title="Copy the clone command">clone by hand</button>';
  }else if(r.need==='manual'){
    const st=r.clone.state;
    what=st==='exists'?'a folder with this name is in the way'
      :st==='no_access'?'the connected GitHub account cannot see it':'clone failed';
    why=esc(r.clone.detail||'')+(st==='exists'?' · move or rename it and XO Space tries again'
      :st==='no_access'?' · ask the owner to add that account; XO Space retries on its own':' · XO Space retries later');
    acts='<button class="shr-copy" type="button" data-act="copy" data-copy="'+esc(cloneCmd(r.repo))+'" title="Copy the clone command">clone by hand</button>';
  }else if(r.need==='cloning'){
    what='cloning into '+esc((sharingStatus()&&sharingStatus().projects_root)||'your XO root');
    why='nothing to do · it joins the list below when done';
    acts='<span class="shl-prog" aria-label="cloning"><i></i></span>';
  }else{
    what='XO Space clones it on the next check';
    why='or clone it yourself:';
    acts='<button class="shr-copy" type="button" data-act="copy" data-copy="'+esc(cloneCmd(r.repo))+'" title="Copy the clone command">clone by hand</button>';
  }
  return'<div class="shl-inbox-row">'
    +'<span class="shl-inbox-top">'+stateChip(r)+'<b>'+esc(r.repo)+'</b></span>'
    +'<span class="shr-muted">'+what+' · '+why+'</span>'
    +'<span class="shl-inbox-acts">'+acts+'</span>'
    +'</div>';
}
function railRow(r){
  const sel=open===r.project;
  return'<div class="shl-row'+(sel?' is-sel':'')+(r.need==='apply'?' is-need':'')+'" id="shl-row-'+esc(r.project)+'">'
    +'<button class="shl-row-head" type="button" data-act="select" data-id="'+esc(r.project)+'" aria-pressed="'+(sel?'true':'false')+'">'
      +'<span class="shl-row-top"><b>'+esc(r.name)+'</b><span class="prj-spacer"></span>'+stateChip(r)+'</span>'
      +'<span class="shl-row-sub"><em>'+esc(r.repo)+'</em><span class="prj-spacer"></span><span class="shr-muted">'+railMeta(r)+'</span></span>'
    +'</button>'
    +(r.need==='apply'?'<span class="shl-row-acts"><button class="sess-refresh is-sm" type="button" data-act="apply" data-id="'+esc(r.project)+'"'
        +(busy.has(r.project)?' disabled':'')+' title="Fast-forward to origin/'+esc(r.c.branch||'main')+'">'+(busy.has(r.project)?'Applying…':'Apply')+'</button></span>':'')
    +'</div>';
}
function railMeta(r){
  if(r.lastError)return'<span class="is-warn" title="'+esc(r.lastError)+'">fetch failed'+(r.lastFetchAt?' · '+esc(rel(r.lastFetchAt)):'')+'</span>';
  const shared=r.others===0?'only you':'shared'+(r.others?' with '+r.others:'');
  return shared+(r.lastFetchAt?' · '+esc(rel(r.lastFetchAt)):'');
}
function railHTML(m){
  const inbox=m.filter(r=>r.incoming),mine=m.filter(r=>r.mine);
  return'<div class="shl-rail">'
    +(inbox.length?'<div class="shl-inbox"><div class="prj-ptitle">Shared with you · not on this machine yet</div>'
      +inbox.map(inboxRow).join('')+'</div>':'')
    +'<div class="shl-sec"><div class="shl-sec-head"><span class="prj-ptitle">Shared projects</span>'
      +'<span class="prj-spacer"></span><span class="shr-muted">'+(actionable(m).filter(r=>r.mine).length?'work waiting first':plural(mine.length,'project'))+'</span></div>'
      +(mine.length?'<div class="shl-rows">'+mine.map(railRow).join('')+'</div>'
        :'<div class="prj-note">nothing shared from this machine yet — “+ Share a project” above.</div>')
    +'</div></div>';
}

/* ── detail ───────────────────────────────────────────────────────────── */
function stateChip(r){
  switch(r.state){
    case'behind':return'<span class="tchip st-shared">'+r.behind+' new · not applied</span>';
    case'sync':return'<span class="tchip st-quiet">in sync</span>';
    case'fetchfail':return'<span class="tchip st-blocked" title="'+esc(r.lastError)+'">fetch failed</span>';
    case'error':return'<span class="tchip st-blocked" title="'+esc(r.c.error)+'">no commits read</span>';
    case'cloning':return'<span class="tchip st-shared">cloning…</span>';
    case'needs_auth':return'<span class="tchip st-blocked">needs GitHub</span>';
    case'no_access':return'<span class="tchip st-blocked">no access</span>';
    case'exists':return'<span class="tchip st-blocked">folder in the way</span>';
    case'available':return'<span class="tchip st-available">shared with you</span>';
    default:return r.incoming?'<span class="tchip st-blocked">clone failed</span>':'<span class="tchip">checking…</span>';
  }
}
function sharedChip(r){
  if(r.others===0)return'<span class="tchip" title="you own this; nobody else can see it">only you</span>';
  return'<span class="tchip st-shared">shared'+(r.others?' with '+r.others:'')+'</span>';
}
/* the rail's selection, defaulting to the first row (work waiting first) so
   the panel is never blank while there is something to show. The default
   waits for the behind counts: before them every row sorts by name, and a
   selection made then would land on the wrong project and stick. */
function selected(m){
  const mine=m.filter(r=>r.mine);
  if(!mine.length){open=null;return null;}
  if(!open||!mine.some(r=>r.project===open)){
    if(!commitsReady)return null;
    open=mine[0].project;
  }
  return mine.find(r=>r.project===open);
}
function detailHTML(r){
  const id=r.project,c=r.c;
  let commitsBody;
  if(!c)commitsBody='<div class="prj-skel is-sm"></div><div class="prj-skel is-sm is-short"></div>';
  else if(!c.ok)commitsBody='<div class="prj-note">'+esc(c.error)+'</div>';
  else if(!c.commits.length)commitsBody='<div class="prj-note">no commits on origin/'+esc(c.branch||'main')+' yet</div>';
  else{
    const behind=c.behind|0;
    commitsBody='<div class="prj-list">'+c.commits.slice(0,5).map((k,i)=>'<div class="prj-li'+(i<behind?' is-new':'')+'">'
      +'<code class="shr-hash">'+esc(shortHash(k.hash))+'</code>'
      +'<span class="shr-subject">'+esc(k.subject)+'</span>'
      +(i<behind?'<span class="tchip st-shared shr-newtag">new</span>':'')
      +'<span class="tmuted">'+esc(k.author)+' · '+rel(k.date)+'</span>'
      +'</div>').join('')+'</div>'
      +(behind>0?'<div class="shr-apply"><span class="shr-muted">or by hand</span><code>'+esc(applyCmd(c.path,c.branch))+'</code>'
        +'<button class="shr-copy" type="button" data-act="copy" data-copy="'+esc(applyCmd(c.path,c.branch))+'" title="Copy merge command">copy</button>'
        +'<span class="shr-muted">XO Space fetches; nothing is merged until you apply.</span></div>':'');
  }
  const behind=c&&c.ok?(c.behind|0):0;
  return'<div class="shl-detail" id="shl-detail">'
    +'<div class="shl-detail-head">'
      +'<div class="shl-detail-title"><span class="shl-name">'+esc(r.name)+'</span>'
        +'<span class="shl-detail-meta"><em>'+esc(r.repo)+'</em>'
          +(c&&c.ok?'<span class="tchip">'+esc(c.branch||'main')+'</span>':'')+sharedChip(r)
          +(r.lastFetchAt?'<span class="shr-muted">checked '+esc(rel(r.lastFetchAt))+'</span>':'')
          +(r.lastError?'<span class="shr-muted is-warn" title="'+esc(r.lastError)+'">fetch failed</span>':'')
          +(r.autoClonedAt?'<span class="shr-muted">cloned by XO Space '+esc(rel(r.autoClonedAt))+'</span>':'')
        +'</span></div>'
      +'<span class="prj-spacer"></span>'
      +(behind>0?'<button class="sess-refresh shl-primary" type="button" data-act="apply" data-id="'+esc(id)+'"'
        +(busy.has(id)?' disabled':'')+'>'+(busy.has(id)?'Applying…':'Apply '+plural(behind,'commit'))+'</button>':'')
      +'<button class="sess-refresh" type="button" data-act="list" data-id="'+esc(id)+'">Open in List</button>'
    +'</div>'
    +'<div class="shl-rule"></div>'
    +'<div class="shl-sec"><div class="shl-sec-head"><span class="prj-ptitle">Commits on origin/'+esc(c&&c.branch||'main')+'</span>'
      +(behind>0?'<span class="tchip st-shared">'+behind+' new · not applied</span>':(c&&c.ok?'<span class="tchip st-quiet">up to date</span>':''))
      +'</div>'+commitsBody+'</div>'
    +'<div class="shl-rule"></div>'
    +'<div class="shl-sec"><div class="shl-sec-head"><span class="prj-ptitle">Members</span>'+sharedChip(r)+'</div>'
      +'<div class="shr-members" id="shl-members-'+esc(id)+'">'+membersHTML(id)+'</div>'
      +'<form class="shr-form" data-form="share" data-id="'+esc(id)+'">'
        +'<input class="tv-filter shr-input" name="ws" placeholder="recipient workspace id" '
          +'autocomplete="off" spellcheck="false" aria-label="Recipient workspace id">'
        +'<button class="sess-refresh shl-primary is-sm" type="submit"'+(busy.has(id)?' disabled':'')+'>Share</button>'
      +'</form>'
      +'<span class="shr-muted">They copy their id from the strip at the top of their own Sharing pane — or send yours with “copy invite”.</span>'
    +'</div>'
  +'</div>';
}
const IDLE_NOTE={
  disabled:'sharing is parked; members appear once it runs',
  unknown:'waiting for the relay to report',
  solo:'not shared yet — share it below',
};
const memberRank=m=>m.role==='owner'?0:m.status==='revoked'?2:1;
function memberRow(m,own,iOwn,id){
  const canRevoke=iOwn&&m.role!=='owner'&&m.status==='active';
  const asking=confirmRevoke&&confirmRevoke.id===id&&confirmRevoke.ws===m.workspace_id;
  return'<div class="shr-row'+(m.status==='revoked'?' is-revoked':'')+'">'
    +'<code class="shr-id" title="'+esc(m.workspace_id)+'">'+esc(shortId(m.workspace_id))+'</code>'
    +'<button class="shr-copy" type="button" data-act="copy" data-copy="'+esc(m.workspace_id)+'" title="Copy workspace id">copy</button>'
    +'<span class="tchip">'+esc(m.role)+'</span>'
    +(m.workspace_id===own?'<span class="tchip st-shared">you</span>':'')
    +(m.status==='revoked'?'<span class="tchip st-blocked">revoked</span>':'')
    +(m.bound===false&&m.status==='active'?'<span class="shr-muted" title="that workspace has not checked in yet">not seen yet</span>':'')
    +(canRevoke?'<span class="shr-act">'+(asking
        /* revoke asks first, in the row; nothing is sent until Confirm */
        ?'<span class="shr-confirm" role="group" aria-label="Confirm revoke"><span class="shr-muted">revoke '+esc(shortId(m.workspace_id))+'?</span>'
          +'<button class="shr-confirm-yes" type="button" data-act="revoke-yes" data-id="'+esc(id)+'" data-ws="'+esc(m.workspace_id)+'">Confirm</button>'
          +'<button class="shr-confirm-no" type="button" data-act="revoke-no">Cancel</button></span>'
        :'<button class="shr-revoke" type="button" data-act="revoke" data-id="'+esc(id)+'" data-ws="'+esc(m.workspace_id)+'">Revoke</button>')
      +'</span>':'')
    +'</div>';
}
function membersHTML(id){
  const st=memberState(id);
  if(st!=='live')return'<div class="prj-note">'+IDLE_NOTE[st]+'</div>';
  const m=members.get(id);
  if(!m)return'<div class="prj-skel is-sm"></div><div class="prj-skel is-sm is-short"></div>';
  if(!m.ok)return'<div class="prj-note">'+esc(m.error)+'</div>';
  const own=m.own,ms=m.members.slice();
  const iOwn=ms.some(x=>x.role==='owner'&&x.workspace_id===own);
  /* "shared" means someone other than the owner can see it: a group that
     holds only you is the empty state, revoked history kept underneath */
  const others=ms.filter(x=>x.status==='active'&&!(x.role==='owner'&&x.workspace_id===own));
  const shown=others.length?ms:ms.filter(x=>x.status==='revoked');
  shown.sort((a,b)=>memberRank(a)-memberRank(b));
  return(others.length?'':'<div class="shr-empty"><b>Not shared with anyone yet</b>'
      +'<p>Paste another workspace’s id below, or send them your invite from the strip.</p></div>')
    +(shown.length?'<div class="shr-rows">'+shown.map(x=>memberRow(x,own,iOwn,id)).join('')+'</div>':'');
}
function paintMembers(id){
  const box=root&&root.querySelector('#shl-members-'+CSS.escape(id));
  if(box)box.innerHTML=membersHTML(id);
}
function emptyCardsHTML(){
  const off=parked();
  const ws=sharingStatus()&&sharingStatus().own_workspace_id;
  return'<div class="shl-empty">'
    +'<div class="shl-empty-card"><b>Share one of your projects</b>'
      +'<p>Pick any project with a git origin and paste the other workspace’s id. Their XO Space clones the repo on its next check, '
      +'and from then on new commits show up here for both of you — one click to apply, no merging by hand.</p>'
      +'<div><button class="sess-refresh shl-primary" type="button" data-act="composer"'+(off?' disabled':'')+'>+ Share a project</button></div></div>'
    +'<div class="shl-empty-card"><b>Receive a project</b>'
      +'<p>Send the owner your invite: one line with your workspace id and what to click. Once they share, the repo shows up here '
      +'as “shared with you” and XO Space clones it into your XO root by itself.</p>'
      +'<div>'+(ws?'<button class="shr-copy is-accent" type="button" data-act="copy" data-copy="'+esc(inviteText())+'">copy invite</button>'
        :'<span class="shr-muted">your workspace id appears here once sharing runs</span>')+'</div></div>'
    +'</div>';
}
function bodyHTML(m){
  const res=sharingStatusRes();
  if(!res)return'<div class="prj-rows"><div class="prj-skel"></div><div class="prj-skel"></div></div>';
  if(!res.ok)return'<div class="prj-empty"><b>Sharing status unavailable</b><p>'+esc(failText(res))+'</p></div>';
  if(parked())return emptyCardsHTML();
  if(!m.length)return(composer?composerHTML():'')+emptyCardsHTML();
  const r=selected(m);
  return'<div class="shl-split">'+railHTML(m)
    +'<div class="shl-main">'+(composer?composerHTML():r?detailHTML(r)
      :m.some(x=>x.mine)?'<div class="shl-detail"><div class="prj-skel is-sm"></div><div class="prj-skel is-sm is-short"></div></div>'
      :'<div class="prj-empty"><b>Nothing shared from this machine yet</b><p>Use “+ Share a project” above, or wait for an incoming repo to finish cloning.</p></div>')
    +'</div></div>';
}

/* ── composer ─────────────────────────────────────────────────────────── */
function pickChip(p){
  const e=entryFor(p.id);
  if(e&&e.shared){
    const n=typeof e.members==='number'?Math.max(0,e.members-1):null;
    return n===0?'<span class="tchip">only you</span>':'<span class="tchip st-shared">shared'+(n?' with '+n:'')+'</span>';
  }
  return p.unscaffolded?'<span class="tchip st-blocked">unscaffolded</span>':'';
}
function picksHTML(){
  const q=(composer.filter||'').trim().toLowerCase();
  const list=catalog.filter(p=>!q||p.id.toLowerCase().includes(q)||String(p.display_name||'').toLowerCase().includes(q));
  if(!catalog.length)return'<div class="prj-note">no projects in this workspace yet</div>';
  if(!list.length)return'<div class="prj-note">no project matches “'+esc(composer.filter)+'”</div>';
  return list.map(p=>'<button class="shl-pick'+(composer.pick===p.id?' is-sel':'')+'" type="button" data-act="pick" data-id="'+esc(p.id)+'" '
    +'aria-pressed="'+(composer.pick===p.id?'true':'false')+'">'
    +'<span class="shl-dot"></span><b>'+esc(p.display_name||p.id)+'</b>'
    +(p.id!==p.display_name&&p.display_name?'<em>'+esc(p.id)+'</em>':'')
    +'<span class="prj-spacer"></span>'+pickChip(p)+'</button>').join('');
}
function composerHTML(){
  const name=composer.pick?(names.get(composer.pick)||composer.pick):'';
  return'<div class="shl-composer" id="shl-composer">'
    +'<div class="shl-composer-title"><b>Share a project</b>'
      +'<span class="shr-muted">Pick a project with a git origin, paste the recipient’s workspace id. Their XO Space clones it on its next check and new commits flow both ways from then on.</span></div>'
    +'<div class="shl-sec"><div class="shl-sec-head"><span class="shl-step">1 · Project</span><span class="prj-spacer"></span>'
      +'<input class="tv-filter" data-filter placeholder="Filter projects…" autocomplete="off" spellcheck="false" aria-label="Filter projects" value="'+esc(composer.filter||'')+'"></div>'
      +'<div class="shl-picks" id="shl-picks">'+picksHTML()+'</div></div>'
    +'<form class="shl-sec" data-form="share-composer">'
      +'<div class="shl-sec-head"><span class="shl-step">2 · Recipient</span></div>'
      +'<div class="shr-form" style="margin-top:0">'
        +'<input class="tv-filter shr-input" name="ws" placeholder="recipient workspace id" autocomplete="off" spellcheck="false" '
          +'aria-label="Recipient workspace id" value="'+esc(composer.ws||'')+'">'
        +'<button class="sess-refresh shl-primary" type="submit" id="shl-composer-go"'+(composer.pick?'':' disabled')+'>'
          +(name?'Share '+esc(name):'Share')+'</button>'
      +'</div>'
      +'<span class="shr-muted">Ask them for the id from the strip on their own Sharing pane — or send them your invite and let them share with you. Sharing again with someone who already has it does nothing.</span>'
    +'</form>'
  +'</div>';
}
function paintComposer(){
  const el=root.querySelector('#shl-composer');
  if(el)el.outerHTML=composerHTML();
}

/* ── events ───────────────────────────────────────────────────────────── */
async function onClick(e){
  const b=e.target.closest('[data-act]');
  if(!b||b.disabled)return;
  const id=b.dataset.id;
  switch(b.dataset.act){
    case'composer':
      composer=composer?null:{pick:null,filter:'',ws:''};
      confirmRevoke=null;
      render();
      if(composer){const f=root.querySelector('[data-filter]');if(f)f.focus();}
      return;
    case'check':return doCheck(b);
    case'copy':
      try{await navigator.clipboard.writeText(b.dataset.copy);toast(b.textContent.trim()==='copy invite'?'invite copied':'copied');}
      catch(err){toast('copy failed — select and copy by hand');}
      return;
    case'select':
      open=id;
      confirmRevoke=null;
      composer=null; /* picking a project answers "what do you want to see" */
      render();
      return;
    case'apply':return doApply(id);
    case'connect':return go('secrets');
    case'list':
      /* views never import each other: switch to List and tell it which
         drawer to open; it parks the request until its catalog is loaded */
      go('projects');
      dispatchEvent(new CustomEvent('space:open-project',{detail:id}));
      return;
    case'pick':
      composer.pick=composer.pick===id?null:id;
      keepComposerInput();
      paintComposer();
      return;
    case'revoke':
      confirmRevoke={id,ws:b.dataset.ws};
      paintMembers(id);
      const yes=root.querySelector('.shr-confirm-yes');if(yes)yes.focus();
      return;
    case'revoke-no':{
      const was=confirmRevoke;confirmRevoke=null;
      if(was)paintMembers(was.id);
      return;
    }
    case'revoke-yes':return doRevoke(id,b.dataset.ws);
  }
}
function keepComposerInput(){
  const ws=root.querySelector('#shl-composer input[name=ws]');
  if(ws&&composer)composer.ws=ws.value;
}
function onSubmit(e){
  const f=e.target.closest('form[data-form]');
  if(!f)return;
  e.preventDefault();
  const ws=(f.querySelector('input[name=ws]').value||'').trim();
  if(f.dataset.form==='share')return doShare(f.dataset.id,ws,f);
  if(f.dataset.form==='share-composer'){
    if(!composer||!composer.pick){toast('pick a project first');return;}
    return doShare(composer.pick,ws,f,true);
  }
}
function onInput(e){
  const f=e.target.closest('[data-filter]');
  if(f&&composer){
    composer.filter=f.value;
    const picks=root.querySelector('#shl-picks');
    if(picks)picks.innerHTML=picksHTML();
  }
}

/* ── writes ───────────────────────────────────────────────────────────── */
async function doShare(id,ws,form,fromComposer){
  if(!ws){toast('enter the recipient’s workspace id');form.querySelector('input[name=ws]').focus();return;}
  const btn=form.querySelector('button[type=submit]');
  btn.disabled=true;
  const res=await share(id,ws);
  btn.disabled=false;
  if(!res.ok){toast('share failed: '+failText(res));return;}
  toast('shared '+(names.get(id)||id)+' with '+shortId(ws));
  members.delete(id);
  if(fromComposer){composer=null;open=id;}
  else form.querySelector('input[name=ws]').value='';
  /* the swarm has it; our relay learns on the nudged tick */
  await refreshSoon();
  render();
}
async function doRevoke(id,ws){
  const slot=root.querySelector('.shr-confirm');
  if(slot)slot.querySelectorAll('button').forEach(b=>{b.disabled=true;});
  const res=await revoke(id,ws);
  confirmRevoke=null;
  if(!res.ok){toast('revoke failed: '+failText(res));paintMembers(id);return;}
  toast('revoked '+shortId(ws));
  members.delete(id);
  await refreshSoon();
  render();
}
async function doApply(id){
  if(busy.has(id))return;
  busy.add(id);
  render();
  const res=await apply(id);
  busy.delete(id);
  if(!res.ok){toast('apply failed: '+failText(res));render();return;}
  const n=res.data.applied|0;
  toast(n?'applied '+plural(n,'commit')+' to '+(names.get(id)||id):'already up to date');
  const c=await fetchCommits(id,5);
  commits.set(id,c.ok?{ok:true,behind:c.data.behind,branch:c.data.branch,path:c.data.path,commits:c.data.commits||[]}
    :{ok:false,error:failText(c)});
  render();
  refreshSoon().then(()=>{if(!editing())render();});
}
async function doCheck(btn){
  btn.disabled=true;
  const res=await checkNow();
  if(!res.ok){btn.disabled=false;toast('check failed: '+failText(res));return;}
  toast('checking…');
  await refreshSoon(1800);
  if(!editing())render();
  loadCommits();
}
