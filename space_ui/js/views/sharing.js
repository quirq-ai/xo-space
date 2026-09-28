import {INBOX_PAGES} from '../core/navigation.js?v=20260915-agents2';
import {setSectionActions} from '../core/section-nav.js?v=20260921-refresh1';
/* Sharing: the project-sharing page in the Space UI (issue #83; redesign
   2026-09-28). Overview first, then details on demand:

     Head         one sentence on what needs you, then the live line (on /
                  parked / failed, last check, watched branch, your id,
                  copy invite).
     Lanes        every project shared from here on a 30-day axis, one dot
                  per commit on origin/<branch>; unapplied ones glow. Work
                  waiting first. Incoming repos ("shared with you", not
                  cloned yet) sit above them with their one thing to click.
     Zoom         the selected lane: commits (pick one), what changed in it
                  (files, +/-, a diff preview), who has been pushing,
                  members + share/revoke, recent sharing events. Apply lives
                  on its header; a refused apply shows git's reason and the
                  by-hand command in place.
     Cards        the other projects; clicking one zooms it.
     Composer     "+ Share a project" swaps into the zoom's place.

   Data comes through views/sharing_data.js (one status poll, the BFF
   calls); this file only paints and handles events. One delegated click /
   submit / input listener on the section: the pane re-renders from state,
   so nothing is bound per element. */
import {openProjectAdd} from '../core/project-actions.js?v=20260914-details1';
import {toast} from '../core/ui.js';
import {esc,rel,shortId,shortHash,sharingStatus,sharingStatusRes,refreshSharingStatus,
  startSharingPoll,refreshSoon,consumeNewClone,REASON,parked,memberState,entryFor,repos,
  cloneCmd,applyCmd,inviteText,fetchCatalog,fetchCommits,fetchChanges,fetchMembers,share,revoke,apply,
  checkNow,failText,recentFor} from './sharing_data.js?v=20260928-lanes1';

const plural=(n,word)=>n.toLocaleString()+' '+word+(n===1?'':'s');
const DAY=864e5,WINDOW_DAYS=30,COMMIT_LIMIT=50,MINUS='−';

let root=null;
let pageActions=null,shareButton=null,checkButton=null,checking=false;
let go=()=>{};            /* ctx.switchTo, captured on mount */
let names=new Map();      /* project id -> display name, from the catalog */
let catalog=[];           /* every project, for the composer's picker */
let commits=new Map();    /* project id -> {ok,behind,branch,path,commits,error} */
let members=new Map();    /* project id -> {ok,own,members,error} */
let open=null;            /* the zoomed project's id */
let composer=null;        /* null | {pick,filter,ws} */
let sharePending=false;   /* keep the active share form until its request settles */
let busy=new Set();       /* project ids with a write in flight */
let confirmRevoke=null;   /* {id,ws} while a revoke waits for Confirm */
let renderedAt=0;
let catalogDirty=false;
let changes=new Map();    /* 'id\nsha\npath' -> {ok,data}|{ok:false,error}; commits never change */
let changesInFlight=new Set();
let pickedCommit=new Map(); /* project id -> sha the person clicked */
let pickedFile=new Map();   /* 'id\nsha' -> path the person clicked */
let applyErr=new Map();     /* project id -> git's reason for the last refused apply */
let lastOpen=null;          /* the zoom rises only when the selection changes */
/* Inbox hands off here ("show this project", issue #142). The request is
   parked until the lanes list the project; once selected, focus moves to
   its Apply button (or the lane) as soon as its commits are in. */
let focusReq=null,focusNow=false;
addEventListener('space:sharing-focus',e=>{
  focusReq=String(e.detail||'')||null;
  if(root&&!editing())render();
});
addEventListener('space:projects-changed',()=>{catalogDirty=true;});
addEventListener('space:project-access-changed',()=>{catalogDirty=true;members.clear();});

export default {
  /* Sharing keeps its own mounted section within the Inbox navigation. */
  ...INBOX_PAGES.find(page=>page.id==='sharing'),
  section:'sharing',
  async mount(el,ctx){
    root=el;
    go=ctx.switchTo;
    if(!pageActions){
      pageActions=document.createElement('div');pageActions.className='sharing-page-actions';
      pageActions.innerHTML='<button class="sess-refresh shl-primary" type="button" data-act="composer">+ Share a project</button>'
        +'<button class="sess-refresh" type="button" data-act="check" title="Ask the relay to check now instead of waiting for the next minute">Check now</button>';
      shareButton=pageActions.querySelector('[data-act="composer"]');checkButton=pageActions.querySelector('[data-act="check"]');
      pageActions.addEventListener('click',onClick);
    }
    renderActions();setSectionActions('sharing',pageActions);
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
  show(){if(root&&(catalogDirty||Date.now()-renderedAt>2000)){catalogDirty=false;return refresh();}},
  refresh,
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
   "N waiting" tracks the relay's fetches. */
let commitsReady=false;   /* the first behind counts are in: safe to pick a default row */
async function loadCommits(){
  const mine=repos().filter(r=>r.mine);
  await Promise.all(mine.map(async r=>{
    const res=await fetchCommits(r.project,COMMIT_LIMIT);
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
  paintSnapshot();
  await loadCommits();
}
/* A poll tick repaints everything unless the person is mid-edit (composer
   open, a revoke waiting for Confirm): then only the head moves, so a
   keystroke or a pending question is never wiped. */
function onStatus(){
  if(!root)return;
  if(consumeNewClone())loadCatalog().then(()=>{if(!editing())render();});
  /* a failed changes read is retried on the next tick, never in a loop */
  for(const [k,v] of changes)if(!v.ok)changes.delete(k);
  paintSnapshot();
  loadCommits();
}
function paintSnapshot(){
  if(!root)return;
  renderActions();
  if(editing()){
    const head=root.querySelector('#shl-head');
    if(head)head.outerHTML=headHTML(model());
  }else render();
}
function editing(){
  const recipient=root?.querySelector('form[data-form="share"] input[name="ws"]');
  return !!composer||!!confirmRevoke||sharePending
    ||!!recipient&&(!!recipient.value||document.activeElement===recipient);
}
/* the zoomed project's detail: commits are already loading; members only
   when the relay says the repo is live */
function ensureDetail(id){
  if(memberState(id)==='live'&&!members.has(id))loadMembers(id);
}
/* the zoom's "what changed": one read per (commit, file), cached for the page */
function ensureChanges(r){
  const sha=selectedSha(r);
  if(!sha)return;
  const path=pickedFile.get(r.project+'\n'+sha)||'';
  const k=r.project+'\n'+sha+'\n'+path;
  if(changes.has(k)||changesInFlight.has(k))return;
  changesInFlight.add(k);
  fetchChanges(r.project,sha,path).then(res=>{
    changesInFlight.delete(k);
    changes.set(k,res.ok?{ok:true,data:res.data}:{ok:false,error:failText(res)});
    if(root&&open===r.project&&!composer){
      const box=root.querySelector('#shl-changes');
      const now=model().find(x=>x.mine&&x.project===r.project);
      if(box&&now)box.outerHTML=changesHTML(now);
    }
  });
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
      state=r.autoCloneSuppressed?'removed':st||'available';
      if(r.autoCloneSuppressed)need='restore';
      else if(st==='needs_auth')need='auth';
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
  renderActions();
  const m=model();
  const again=focusKey();
  root.querySelector('.prj').innerHTML=headHTML(m)+bodyHTML(m);
  if(again){const el=root.querySelector(again);if(el)el.focus({preventScroll:true});}
  renderedAt=Date.now();
  if(open&&!composer){
    ensureDetail(open);
    const r=m.find(x=>x.mine&&x.project===open);
    if(r&&r.c&&r.c.ok)ensureChanges(r);
  }
  if(focusNow&&!composer&&root.classList.contains('is-active')){
    const r=m.find(x=>x.mine&&x.project===open);
    if(r&&r.c){focusNow=false;revealSelected();}
  }
}
/* A repaint replaces every node; the focused control (by action, id and
   panel) gets focus back, so a poll tick or a refresh never drops it. */
function focusKey(){
  const el=document.activeElement;
  if(!el||!root.contains(el)||!el.dataset||!el.dataset.act)return null;
  const scope=el.closest('#shl-detail')?'#shl-detail ':el.closest('.shl-lanes')?'.shl-lanes ':el.closest('.shl-cards')?'.shl-cards ':'';
  return scope+'[data-act="'+CSS.escape(el.dataset.act)+'"]'+(el.dataset.id?'[data-id="'+CSS.escape(el.dataset.id)+'"]':'');
}
function revealSelected(){
  const row=root.querySelector('#shl-row-'+CSS.escape(open));
  if(row)row.scrollIntoView({block:'nearest'});
  const detail=root.querySelector('#shl-detail');
  if(detail){
    detail.scrollIntoView({block:'start',behavior:'smooth'});
    detail.classList.add('is-flash');
  }
  const target=root.querySelector('#shl-detail [data-act="apply"]')||row;
  if(target)target.focus({preventScroll:true});
}
function renderActions(){
  if(!pageActions)return;
  const status=sharingStatusRes(),off=parked()||!status||!status.ok;
  shareButton.textContent=composer?'Cancel':'+ Share a project';
  shareButton.classList.toggle('shl-primary',!composer);shareButton.disabled=sharePending||(!composer&&off);
  checkButton.disabled=off||checking;checkButton.textContent=checking?'Checking…':'Check now';
  if(checking)checkButton.setAttribute('aria-busy','true');else checkButton.removeAttribute('aria-busy');
}

/* ── head: one sentence, then the live line ──────────────────────────── */
function headline(m){
  const res=sharingStatusRes();
  if(!res)return'Checking sharing…';
  if(!res.ok)return'Sharing status is unavailable.';
  if(parked())return'Sharing is parked.';
  if(!m.length)return'Nothing is shared yet.';
  const news=m.filter(r=>r.need==='apply').length;
  if(news===1)return'One project has news for you.';
  if(news>1)return news+' projects have news for you.';
  if(actionable(m).length)return'A shared project needs you.';
  return'Everything is in sync.';
}
function headHTML(m){
  const status=sharingStatus(),res=sharingStatusRes();
  let pulse='shl-pulse is-off',line;
  if(!res)line='checking…';
  else if(!res.ok){pulse='shl-pulse is-bad';line=esc(failText(res));}
  else if(status.cadence==='parked')line=esc(REASON[status.reason]||'parked');
  else{
    const ok=status.last_poll_ok!==false;
    pulse=ok?'shl-pulse':'shl-pulse is-bad';
    line=(ok?'live':'last check failed')+' · last check '+(status.last_poll_at?esc(rel(status.last_poll_at)):'pending')
      +' · every minute · watching '+esc(status.watch_branch||'main');
  }
  const ws=status&&status.own_workspace_id;
  const right=ws
    ?'<span class="shr-right"><span class="shr-muted">your id</span><code class="shr-id" title="'+esc(ws)+'">'+esc(shortId(ws))+'</code>'
      +'<button class="shr-copy" type="button" data-act="copy" data-copy="'+esc(ws)+'" title="Copy workspace id">copy</button>'
      +'<button class="shr-copy is-accent" type="button" data-act="copy" data-copy="'+esc(inviteText())+'" '
        +'title="Copy a one-line invite: what to click and your id">copy invite</button></span>'
    :'';
  return'<div class="shl-head" id="shl-head"><h2 class="shl-headline">'+esc(headline(m))+'</h2>'
    +'<div class="shl-live"><span class="'+pulse+'" aria-hidden="true"></span><span>'+line+'</span>'+right+'</div></div>';
}

/* ── incoming + lanes + cards ────────────────────────────────────────── */
/* An incoming repo's row says where the clone stands and, when only a
   person can move it on, offers the one thing to click. */
function inboxRow(r){
  let what='',why='',acts='';
  if(r.need==='restore'){
    what='removed from this Space';
    why='automatic cloning is paused';
    acts='<button class="sess-refresh is-sm" type="button" data-act="restore">Clone project</button>';
  }else if(r.need==='auth'){
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
    why='nothing to do · it joins the lanes below when done';
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
function incomingHTML(inbox){
  return'<div class="shl-inbox"><div class="prj-ptitle">Shared with you · not on this machine yet</div>'
    +inbox.map(inboxRow).join('')+'</div>';
}
function railMeta(r){
  if(r.lastError)return'<span class="is-warn" title="'+esc(r.lastError)+'">fetch failed'+(r.lastFetchAt?' · '+esc(rel(r.lastFetchAt)):'')+'</span>';
  const shared=r.others===0?'only you':'shared'+(r.others?' with '+r.others:'');
  return shared+(r.lastFetchAt?' · '+esc(rel(r.lastFetchAt)):'');
}
const ageDays=(k,now)=>{const t=Date.parse(k.date);return Number.isFinite(t)?(now-t)/DAY:Infinity;};
/* commits shown for a project: the 30-day window as a prefix of the list
   (index < behind stays "new"), never fewer than 5 or than the unapplied */
function windowed(c,now=Date.now()){
  if(!c||!c.ok)return[];
  let n=0;
  while(n<c.commits.length&&ageDays(c.commits[n],now)<=WINDOW_DAYS)n++;
  return c.commits.slice(0,Math.max(n,Math.min(5,c.commits.length),c.behind|0));
}
function laneState(r,dots){
  switch(r.state){
    case'behind':return'<span class="is-accent">'+r.behind+' waiting</span>';
    case'sync':return dots?'in sync':'quiet';
    case'fetchfail':return'<span class="is-warn" title="'+esc(r.lastError)+'">fetch failed</span>';
    case'error':return'<span class="is-warn" title="'+esc(r.c.error)+'">no commits read</span>';
    default:return'checking…';
  }
}
function laneRow(r,now){
  const sel=open===r.project,behind=r.behind|0;
  const list=r.c&&r.c.ok?r.c.commits:[];
  let dots=0;
  const marks=list.map((k,i)=>{
    const age=ageDays(k,now);
    if(age<0||age>WINDOW_DAYS)return'';
    dots++;
    return'<i class="shl-dot'+(i<behind?' is-new':'')+'" style="left:'+(100-age/WINDOW_DAYS*100).toFixed(2)+'%" title="'+esc(k.subject)+'"></i>';
  }).join('');
  return'<button class="shl-lane'+(sel?' is-sel':'')+(r.need==='apply'?' is-need':'')+'" id="shl-row-'+esc(r.project)+'" type="button" '
    +'data-act="select" data-id="'+esc(r.project)+'" aria-pressed="'+(sel?'true':'false')+'">'
    +'<span class="shl-lane-name">'+esc(r.name)+'</span>'
    +'<span class="shl-track" aria-hidden="true">'+marks+'</span>'
    +'<span class="shl-lane-state">'+laneState(r,dots)+'</span></button>';
}
function lanesHTML(mine){
  const now=Date.now();
  return'<div class="shl-lanes" role="group" aria-label="Shared projects, last 30 days">'
    +'<div class="shl-axis"><span class="prj-ptitle">Last 30 days</span>'
      +'<span class="shl-ticks" aria-hidden="true"><span>30d</span><span>3w</span><span>2w</span><span>1w</span><span class="is-now">now</span></span>'
      +'<span class="prj-ptitle shl-axis-end">Your copy</span></div>'
    +'<div class="shl-lanes-body">'+mine.map(r=>laneRow(r,now)).join('')+'</div></div>';
}
function cardsHTML(rest){
  if(!rest.length)return'';
  return'<div class="shl-cards">'+rest.map(r=>{
    const k=r.c&&r.c.ok&&r.c.commits[0];
    return'<button class="shl-card" type="button" data-act="select" data-id="'+esc(r.project)+'">'
      +'<span class="shl-card-top"><b>'+esc(r.name)+'</b><span class="prj-spacer"></span>'+stateChip(r)+'</span>'
      +'<span class="shl-card-last">'+(k?esc(k.subject):'no commits read yet')+'</span>'
      +'<span class="shr-muted">'+(k?[esc(k.author),rel(k.date)].filter(Boolean).join(' · '):railMeta(r))+'</span></button>';
  }).join('')+'</div>';
}

/* ── zoom ─────────────────────────────────────────────────────────────── */
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
    case'removed':return'<span class="tchip st-quiet">removed locally</span>';
    default:return r.incoming?'<span class="tchip st-blocked">clone failed</span>':'<span class="tchip">checking…</span>';
  }
}
function sharedChip(r){
  if(r.others===0)return'<span class="tchip" title="you own this; nobody else can see it">only you</span>';
  return'<span class="tchip st-shared">shared'+(r.others?' with '+r.others:'')+'</span>';
}
/* the lanes' selection, defaulting to the first lane (work waiting first) so
   the zoom is never blank while there is something to show. The default
   waits for the behind counts: before them every row sorts by name, and a
   selection made then would land on the wrong project and stick. */
function selected(m){
  const mine=m.filter(r=>r.mine);
  if(focusReq){
    if(mine.some(r=>r.project===focusReq)){open=focusReq;focusReq=null;focusNow=true;}
    /* the first reads are in and it is not shared from here: open the page as is */
    else if(commitsReady)focusReq=null;
  }
  if(!mine.length){open=null;return null;}
  if(!open||!mine.some(r=>r.project===open)){
    if(!commitsReady)return null;
    open=mine[0].project;
  }
  return mine.find(r=>r.project===open);
}
function selectedSha(r){
  const list=windowed(r.c);
  if(!list.length)return null;
  const want=pickedCommit.get(r.project);
  return list.some(k=>k.hash===want)?want:list[0].hash;
}
function detailHTML(r){
  const id=r.project,c=r.c;
  const behind=c&&c.ok?(c.behind|0):0,branch=c&&c.ok&&c.branch||'main';
  const err=applyErr.get(id);
  const rise=lastOpen!==id;lastOpen=id;
  return'<div class="shl-detail'+(rise?' is-rise':'')+'" id="shl-detail">'
    +'<div class="shl-detail-head">'
      +'<div class="shl-detail-title"><span class="shl-name">'+esc(r.name)+'</span>'
        +'<span class="shl-detail-meta"><em>'+esc(r.repo)+'</em>'
          +(c&&c.ok?'<span class="tchip">'+esc(branch)+'</span>':'')+sharedChip(r)
          +(r.lastFetchAt?'<span class="shr-muted">checked '+esc(rel(r.lastFetchAt))+'</span>':'')
          +(r.lastError?'<span class="shr-muted is-warn" title="'+esc(r.lastError)+'">fetch failed</span>':'')
          +(r.autoClonedAt?'<span class="shr-muted">cloned by XO Space '+esc(rel(r.autoClonedAt))+'</span>':'')
        +'</span></div>'
      +'<span class="prj-spacer"></span>'
      +(behind>0?'<button class="sess-refresh shl-primary" type="button" data-act="apply" data-id="'+esc(id)+'"'
        +(busy.has(id)?' disabled':'')+'>'+(busy.has(id)?'Applying…':'Apply '+plural(behind,'commit'))+'</button>'
        :(c&&c.ok?'<span class="shl-uptodate">Your copy is up to date</span>':''))
      +'<button class="sess-refresh" type="button" data-act="list" data-id="'+esc(id)+'">Open in List</button>'
    +'</div>'
    /* a refused apply is when the by-hand command matters: say why, in place */
    +(err&&c&&c.ok?'<div class="shl-callout is-warn"><b>Apply was refused</b><span>'+esc(err)+'</span>'
      +'<span class="shr-apply"><span class="shr-muted">or by hand</span><code>'+esc(applyCmd(c.path,c.branch))+'</code>'
      +'<button class="shr-copy" type="button" data-act="copy" data-copy="'+esc(applyCmd(c.path,c.branch))+'" title="Copy merge command">copy</button></span></div>':'')
    +'<div class="shl-cols">'+commitsHTML(r)+changesHTML(r)+sideHTML(r)+'</div>'
  +'</div>';
}
function commitsHTML(r){
  const id=r.project,c=r.c,branch=c&&c.ok&&c.branch||'main',behind=c&&c.ok?(c.behind|0):0;
  let body;
  if(!c)body='<div class="prj-skel is-sm"></div><div class="prj-skel is-sm is-short"></div>';
  else if(!c.ok)body='<div class="prj-note">'+esc(c.error)+'</div>';
  else if(!c.commits.length)body='<div class="prj-note">no commits on origin/'+esc(branch)+' yet</div>';
  else{
    const cur=selectedSha(r);
    body='<div class="shl-commits">'+windowed(c).map((k,i)=>'<button class="shl-commit'+(k.hash===cur?' is-sel':'')+(i<behind?' is-new':'')+'" type="button" '
      +'data-act="commit" data-id="'+esc(id)+'" data-sha="'+esc(k.hash)+'" aria-pressed="'+(k.hash===cur?'true':'false')+'">'
      +'<span class="shr-subject">'+esc(k.subject)+(i<behind?'<span class="tchip st-shared shr-newtag">new</span>':'')+'</span>'
      /* rel() is '' for a commit with no date: no dangling separator then */
      +'<span class="tmuted"><code class="shr-hash">'+esc(shortHash(k.hash))+'</code> '+[esc(k.author),rel(k.date)].filter(Boolean).join(' · ')+'</span>'
      +'</button>').join('')+'</div>';
  }
  return'<div class="shl-sec shl-col"><div class="shl-sec-head"><span class="prj-ptitle">Commits on origin/'+esc(branch)+'</span>'
    +(behind>0?'<span class="tchip st-shared">'+behind+' new</span>':(c&&c.ok?'<span class="tchip st-quiet">up to date</span>':''))
    +'</div>'+body+'</div>';
}
function changesHTML(r){
  const id=r.project,c=r.c,sha=selectedSha(r);
  const byHand=c&&c.ok&&(c.behind|0)>0
    ?'<button class="shr-link" type="button" data-act="copy" data-copy="'+esc(applyCmd(c.path,c.branch))+'">or by hand: copy the git command</button>':'';
  let body,sum='';
  if(!sha)body='<div class="prj-note">pick a commit to see what it changed</div>';
  else{
    const path=pickedFile.get(id+'\n'+sha)||'';
    const ch=changes.get(id+'\n'+sha+'\n'+path);
    if(!ch)body='<div class="prj-skel is-sm"></div><div class="prj-skel is-sm is-short"></div>';
    else if(!ch.ok)body='<div class="prj-note">could not read this commit’s changes: '+esc(ch.error)+'</div>';
    else{
      const d=ch.data,files=d.files||[],shown=d.diff&&d.diff.path;
      const adds=files.reduce((s,f)=>s+(f.additions||0),0),dels=files.reduce((s,f)=>s+(f.deletions||0),0);
      sum='<span class="shr-muted">'+plural(files.length,'file')+(d.files_truncated?'+':'')
        +' · <span class="shl-add">+'+adds+'</span> <span class="shl-del">'+MINUS+dels+'</span></span>';
      const list=files.map(f=>'<button class="shl-file'+(f.path===shown?' is-sel':'')+'" type="button" data-act="file" '
        +'data-id="'+esc(id)+'" data-sha="'+esc(sha)+'" data-path="'+esc(f.path)+'" aria-pressed="'+(f.path===shown?'true':'false')+'">'
        +'<span>'+esc(f.path)+'</span><span class="prj-spacer"></span>'
        +(f.binary?'<span class="shr-muted">binary</span>':'<span class="shl-add">+'+f.additions+'</span><span class="shl-del">'+MINUS+f.deletions+'</span>')
        +'</button>').join('');
      let diff='';
      if(d.diff&&d.diff.binary)diff='<div class="prj-note">'+esc(d.diff.path)+' is a binary file: no text diff</div>';
      else if(d.diff)diff='<div class="shl-diffbox"><div class="shl-diffname">'+esc(d.diff.path)+'</div><pre class="shl-diff">'
        +d.diff.text.split('\n').map(l=>'<span class="'+(l[0]==='+'?'is-add':l[0]==='-'?'is-del':l.startsWith('@@')?'is-hunk':'')+'">'+esc(l)+'</span>').join('')
        +'</pre>'+(d.diff.truncated?'<div class="prj-note">preview cut at 400 lines</div>':'')+'</div>';
      body=(files.length?'<div class="shl-files">'+list+'</div>':'<div class="prj-note">this commit changed no files</div>')+diff;
    }
  }
  return'<div class="shl-sec shl-col" id="shl-changes"><div class="shl-sec-head"><span class="prj-ptitle">What changed</span>'+sum
    +'<span class="prj-spacer"></span>'+byHand+'</div>'+body+'</div>';
}
function sideHTML(r){
  const id=r.project;
  const counts=new Map();
  for(const k of windowed(r.c))counts.set(k.author,(counts.get(k.author)||0)+1);
  const top=[...counts].sort((a,b)=>b[1]-a[1]),max=top.length?top[0][1]:1;
  const authors=top.length
    ?'<div class="shl-authors">'+top.map(([name,n])=>'<div class="shl-author"><span class="shl-author-top"><span>'+esc(name||'unknown')+'</span>'
      +'<span class="prj-spacer"></span><span class="shr-muted">'+plural(n,'commit')+'</span></span>'
      +'<span class="shl-bar" style="width:'+Math.round(n/max*100)+'%"></span></div>').join('')+'</div>'
    :'<div class="prj-note">no commits in the last 30 days</div>';
  const events=recentFor(r.repo).slice(0,5);
  const history=events.length
    ?events.map(e=>'<div class="shl-event"><span>'+esc(e.label)+(e.detail?' · '+esc(e.detail):'')+'</span>'
      +'<span class="shr-muted">'+esc(rel(e.at))+'</span></div>').join('')
    :'<div class="prj-note">No recent sharing events</div>';
  return'<div class="shl-col shl-side">'
    +'<div class="shl-sec"><div class="shl-sec-head"><span class="prj-ptitle">Who’s been pushing</span></div>'+authors+'</div>'
    +'<div class="shl-sec"><div class="shl-sec-head"><span class="prj-ptitle">Members</span>'+sharedChip(r)+'</div>'
      +'<div class="shr-members" id="shl-members-'+esc(id)+'">'+membersHTML(id)+'</div>'
      +'<form class="shr-form" data-form="share" data-id="'+esc(id)+'">'
        +'<input class="tv-filter shr-input" name="ws" placeholder="recipient workspace id" '
          +'autocomplete="off" spellcheck="false" aria-label="Recipient workspace id">'
        +'<button class="sess-refresh shl-primary is-sm" type="submit"'+(busy.has(id)?' disabled':'')+'>Share</button>'
      +'</form>'
      +'<span class="shr-muted">They copy their id from the top of their own Sharing page, or send yours with “copy invite”.</span>'
    +'</div>'
    +'<div class="shl-sec"><div class="shl-sec-head"><span class="prj-ptitle">Recent sharing events</span></div>'+history+'</div>'
  +'</div>';
}
const IDLE_NOTE={
  disabled:'sharing is parked; members appear once it runs',
  unknown:'waiting for the relay to report',
  solo:'not shared yet: share it below',
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
      +'<p>Paste another workspace’s id below, or send them your invite from the top of this page.</p></div>')
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
      +'and from then on new commits show up here for both of you: one click to apply, no merging by hand.</p>'
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
  const inbox=m.filter(x=>x.incoming),mine=m.filter(x=>x.mine);
  return(inbox.length?incomingHTML(inbox):'')
    +(mine.length?lanesHTML(mine):'')
    +'<div class="shl-main">'+(composer?composerHTML():r?detailHTML(r)
      :mine.length?'<div class="shl-detail"><div class="prj-skel is-sm"></div><div class="prj-skel is-sm is-short"></div></div>'
      :'<div class="prj-empty"><b>Nothing shared from this machine yet</b><p>Use “+ Share a project” above, or wait for an incoming repo to finish cloning.</p></div>')
    +'</div>'
    +(composer?'':cardsHTML(mine.filter(x=>x.project!==open)));
}

/* ── composer ─────────────────────────────────────────────────────────── */
function pickChip(p){
  const e=entryFor(p.id);
  if(e&&e.shared){
    /* a group nobody else can see (last member revoked) is not "shared";
       the picker shows it as a plain project you can share again */
    const n=typeof e.members==='number'?Math.max(0,e.members-1):null;
    if(n!==0)return'<span class="tchip st-shared">shared'+(n?' with '+n:'')+'</span>';
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
    +'<span class="shl-dot-pick"></span><b>'+esc(p.display_name||p.id)+'</b>'
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
        +'<button class="sess-refresh shl-primary" type="submit" id="shl-composer-go"'+(composer.pick&&!sharePending?'':' disabled')+'>'
          +(name?'Share '+esc(name):'Share')+'</button>'
      +'</div>'
      +'<span class="shr-muted">Ask them for the id from the top of their own Sharing page, or send them your invite and let them share with you. Sharing again with someone who already has it does nothing.</span>'
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
      if(sharePending)return;
      composer=composer?null:{pick:null,filter:'',ws:''};
      confirmRevoke=null;
      render();
      if(composer){const f=root.querySelector('[data-filter]');if(f)f.focus();}
      return;
    case'check':return doCheck();
    case'copy':
      try{await navigator.clipboard.writeText(b.dataset.copy);toast(b.textContent.trim()==='copy invite'?'invite copied':'copied');}
      catch(err){toast('copy failed: select and copy by hand');}
      return;
    case'select':{
      if(sharePending)return;
      const fromCard=!!b.closest('.shl-cards');
      open=id;focusReq=null;focusNow=false;
      confirmRevoke=null;
      pickedCommit.delete(id);
      composer=null; /* picking a project answers "what do you want to see" */
      render();
      /* the clicked card is gone after the repaint: keep keyboard focus on
         the project's lane, and bring the zoom into view */
      if(fromCard){
        const lane=root.querySelector('#shl-row-'+CSS.escape(id));if(lane)lane.focus({preventScroll:true});
        const d=root.querySelector('#shl-detail');if(d)d.scrollIntoView({block:'start',behavior:'smooth'});
      }
      return;
    }
    case'commit':
      pickedCommit.set(id,b.dataset.sha);
      render();
      return;
    case'file':
      pickedFile.set(id+'\n'+b.dataset.sha,b.dataset.path);
      render();
      return;
    case'apply':return doApply(id);
    case'connect':return go('setup/connectors');
    case'restore':
      return openProjectAdd(go);
    case'list':
      /* views never import each other: switch to List and tell it which
         drawer to open; it parks the request until its catalog is loaded */
      go('projects/data/list');
      dispatchEvent(new CustomEvent('space:open-project',{detail:id}));
      return;
    case'pick':
      if(sharePending)return;
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
  const recipient=e.target.closest('input[name="ws"]');
  if(composer&&recipient)composer.ws=recipient.value;
  const f=e.target.closest('[data-filter]');
  if(f&&composer){
    composer.filter=f.value;
    const picks=root.querySelector('#shl-picks');
    if(picks)picks.innerHTML=picksHTML();
  }
}

/* ── writes ───────────────────────────────────────────────────────────── */
async function doShare(id,ws,form,fromComposer){
  if(sharePending)return;
  if(fromComposer&&!catalog.some(project=>project.id===id)){
    toast('This project is no longer in the project list. Choose another project.');
    return;
  }
  if(!ws){toast('enter the recipient’s workspace id');form.querySelector('input[name=ws]').focus();return;}
  const btn=form.querySelector('button[type=submit]');
  sharePending=true;btn.disabled=true;renderActions();
  let res;
  try{res=await share(id,ws);}
  finally{
    sharePending=false;btn.disabled=false;
    /* A concurrent action may have painted a new composer while this POST
       was pending. Restore the live button as well as the original node. */
    const current=root?.querySelector('#shl-composer-go');
    if(current)current.disabled=!composer?.pick||!catalog.some(project=>project.id===composer.pick);
    renderActions();
  }
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
  if(!res.ok){applyErr.set(id,failText(res));toast('apply failed: '+failText(res));render();return;}
  applyErr.delete(id);
  const n=res.data.applied|0;
  toast(n?'applied '+plural(n,'commit')+' to '+(names.get(id)||id):'already up to date');
  const c=await fetchCommits(id,COMMIT_LIMIT);
  commits.set(id,c.ok?{ok:true,behind:c.data.behind,branch:c.data.branch,path:c.data.path,commits:c.data.commits||[]}
    :{ok:false,error:failText(c)});
  render();
  refreshSoon().then(()=>{if(!editing())render();});
}
async function doCheck(){
  if(checking)return;
  const restoreFocus=document.activeElement===checkButton;
  checking=true;renderActions();
  try{
    const res=await checkNow();
    if(!res.ok){toast('check failed: '+failText(res));return;}
    toast('checking…');
    await refreshSoon(1800);
    if(!editing())render();
    loadCommits();
  }finally{
    checking=false;renderActions();
    if(restoreFocus&&!checkButton.disabled&&root.classList.contains('is-active')&&document.activeElement===document.body)checkButton.focus({preventScroll:true});
  }
}
