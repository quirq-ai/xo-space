/* Sharing — the fourth Files lens, beside List, Graph and Tree (issue #83).

   One place that answers, without opening each project in turn: which of
   my projects are shared, which have new commits waiting, and what was
   shared with me. The List lens still carries the per-project detail in
   its drawer (commits, apply command, members, share form); a row here
   opens that drawer.

   Data: the same GET /api/project-sharing/status snapshot the List reads,
   through the shared helper (one poll, two subscribers), plus one
   GET /api/xo-projects/{id}/commits?limit=1 per shared project for the
   branch and the behind count — the snapshot does not carry them. Rows
   fill independently, the way drawer panels do: one dead repo costs one
   cell, not the lens. The project catalog supplies display names and is
   the same single-flighted request the List makes. */
import {API_BASE,apiFetch} from '../core/api.js';
import {sharingStripHTML,sharedWithYouHTML,bindSharingCopies,bindSharingActions,
  refreshSharingStatus,startSharingPoll,setSharingNav,sharedProjects,sharedWithYouCount,
  sharingStatus,sharingStatusRes,sharingRel}
  from './projects_sharing.js?v=20260910-sharinglens1';

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const dtfmt=iso=>iso?new Date(iso).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}):'—';
const plural=(n,word)=>n.toLocaleString()+' '+word+(n===1?'':'s');

let root=null;
let go=()=>{};          /* ctx.switchTo, captured on mount */
let names=new Map();    /* project id -> display name, from the catalog */
let commits=new Map();  /* project id -> {ok,behind,branch,error} per row */
let rowKey='';          /* the shared set on screen; a change re-renders */
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
    setSharingNav(ctx.switchTo);
    startSharingPoll(onStatus);
    await Promise.all([loadNames(),refreshSharingStatus()]);
    render();
  },
  /* Coming back to the lens re-reads; right after mount the paint is fresh
     and the second read would only repeat it. */
  show(){if(root&&Date.now()-renderedAt>2000)refresh();}
};

const skeleton=()=>'<div class="prj-head"></div><div class="prj-rows">'
  +'<div class="prj-skel"></div>'.repeat(3)+'</div>';

async function loadNames(){
  const res=await apiFetch(API_BASE+'/api/xo-projects');
  if(!res.ok)return; /* rows fall back to the id; the lens still works */
  names=new Map((res.data.items||[]).map(p=>[p.id,p.display_name||p.id]));
}
async function refresh(){
  const btn=root.querySelector('#shl-refresh');
  if(btn){btn.disabled=true;btn.classList.add('is-busy');}
  await Promise.all([loadNames(),refreshSharingStatus()]);
  render();
}

/* ── model ────────────────────────────────────────────────────────────── */
const behindOf=id=>{const c=commits.get(id);return c&&c.ok&&typeof c.behind==='number'?c.behind:-1;};
function rows(){
  const list=sharedProjects().map(r=>({...r,name:names.get(r.project)||r.project}));
  /* work waiting first, then by name; unknown behind sorts with zero */
  list.sort((a,b)=>Math.max(0,behindOf(b.project))-Math.max(0,behindOf(a.project))
    ||a.name.localeCompare(b.name));
  return list;
}

/* ── header ───────────────────────────────────────────────────────────── */
function summary(list){
  const status=sharingStatus(),res=sharingStatusRes();
  if(!res)return'checking…';
  if(!res.ok)return'sharing status unavailable';
  if(status.cadence==='parked')return'sharing parked';
  const behindN=list.filter(r=>behindOf(r.project)>0).length;
  const inbox=sharedWithYouCount();
  return plural(list.length,'shared project')
    +(behindN?' · '+behindN+' with new commits':'')
    +(inbox?' · '+inbox+' shared with you':'');
}
function head(list){
  return'<div class="prj-head">'
    +'<span class="prj-eyebrow" id="shl-count">'+esc(summary(list))+'</span>'
    +'<span class="prj-spacer"></span>'
    +'<button class="sess-refresh" id="shl-refresh" title="Re-read sharing status">'
      +'&#8635; Refresh</button>'
  +'</div>';
}

/* ── rows ─────────────────────────────────────────────────────────────── */
function branchCell(id){
  const c=commits.get(id);
  if(!c)return'<span class="prj-skel is-sm is-cell"></span>';
  if(!c.ok)return'<span class="shl-muted" title="'+esc(c.error)+'">'+esc(c.error)+'</span>';
  const chip=c.behind>0
    ?'<span class="tchip st-shared">'+c.behind+' new · not applied</span>'
    :(c.behind===0?'<span class="tchip st-quiet">up to date</span>':'');
  return'<span class="shl-branch">'+esc(c.branch||'main')+'</span>'+chip;
}
function membersCell(r){
  return'<span class="tchip st-shared">shared'+(r.others?' with '+r.others:'')+'</span>';
}
function whenCell(r){
  if(r.lastError)return'<span class="shl-cell shl-when is-err" title="'+esc(r.lastError)+'">fetch failed'
    +(r.lastFetchAt?' · '+esc(sharingRel(r.lastFetchAt)):'')+'</span>';
  if(r.lastFetchAt)return'<span class="shl-cell shl-when" title="'+esc(dtfmt(r.lastFetchAt))+'">'
    +esc(sharingRel(r.lastFetchAt))+'</span>';
  return'<span class="shl-cell shl-when is-none">pending</span>';
}
function rowHTML(r){
  const behind=behindOf(r.project)>0;
  const fresh=r.autoClonedAt&&(Date.now()-new Date(r.autoClonedAt).getTime())<86400*1000;
  return'<button class="shl-row'+(behind?' is-behind':'')+'" type="button" '
      +'id="shl-row-'+esc(r.project)+'" data-open="'+esc(r.project)+'" '
      +'title="Open '+esc(r.name)+' in List">'
    +'<span class="shl-name"><b>'+esc(r.name)+'</b>'
      +'<em>'+esc(r.repo)+(fresh?' · auto-cloned '+esc(sharingRel(r.autoClonedAt)):'')+'</em></span>'
    +'<span class="shl-cell shl-branch-cell" id="shl-br-'+esc(r.project)+'">'+branchCell(r.project)+'</span>'
    +'<span class="shl-cell shl-members">'+membersCell(r)+'</span>'
    +whenCell(r)
  +'</button>';
}
function emptyHTML(){
  const status=sharingStatus(),res=sharingStatusRes();
  if(res&&!res.ok)return'<div class="prj-empty"><b>Sharing status unavailable</b>'
    +'<p>'+esc(res.offline?'xo-space is unreachable.':res.error)+'</p></div>';
  if(status&&status.cadence==='parked')return'<div class="prj-empty"><b>Sharing is parked</b>'
    +'<p>The relay is not checking anything until the reason in the strip above is fixed. '
    +'Nothing is lost: shared repos and members are held at the swarm and appear on the first successful check.</p></div>';
  if(sharedWithYouCount())return''; /* the inbox is the content */
  return'<div class="prj-empty"><b>Nothing shared yet</b>'
    +'<p>Open a project in <button class="shl-link" type="button" data-go-list>List</button>, then use its '
    +'Sharing panel to share the repo with another workspace id. Projects shared with you appear here '
    +'as soon as the relay sees them.</p></div>';
}
function rowsHTML(list){
  if(!list.length)return emptyHTML();
  return'<div class="shl-cols" aria-hidden="true">'
      +'<span>Project</span><span>origin / branch</span><span>Members</span><span>Last check</span></div>'
    +'<div class="prj-rows shl-rows">'+list.map(rowHTML).join('')+'</div>';
}

/* ── paint ────────────────────────────────────────────────────────────── */
function render(){
  const list=rows();
  rowKey=list.map(r=>r.project).join('\n');
  root.querySelector('.prj').innerHTML=
    head(list)+sharingStripHTML()
    +'<div class="prj-body">'+sharedWithYouHTML()+rowsHTML(list)+'</div>';
  renderedAt=Date.now();
  bind();
  fillCommits(list);
}
function bind(){
  const r=root.querySelector('#shl-refresh');
  if(r)r.addEventListener('click',refresh);
  bindSharingCopies(root);
  bindSharingActions(root);
  root.querySelectorAll('[data-open]').forEach(b=>b.addEventListener('click',()=>openInList(b.dataset.open)));
  const l=root.querySelector('[data-go-list]');
  if(l)l.addEventListener('click',()=>go('projects'));
}
/* Views never import each other: switch to List and tell it which drawer
   to open. The List parks the request until its catalog is loaded. */
function openInList(id){
  go('projects');
  dispatchEvent(new CustomEvent('space:open-project',{detail:id}));
}
/* One request per row, no barrier: a row's cell fills when its answer
   lands, and a failed one says why in that cell alone. The eyebrow and the
   order settle once every answer is in. */
async function fillCommits(list){
  const key=rowKey;
  await Promise.all(list.map(async r=>{
    const res=await apiFetch(API_BASE+'/api/xo-projects/'+encodeURIComponent(r.project)+'/commits?limit=1');
    commits.set(r.project,res.ok
      ?{ok:true,behind:res.data.behind,branch:res.data.branch}
      :{ok:false,error:res.notImplemented?'not available':res.offline?'xo-space is unreachable':res.error});
    if(rowKey!==key)return; /* re-rendered while in flight */
    const cell=document.getElementById('shl-br-'+r.project);
    if(cell)cell.innerHTML=branchCell(r.project);
    const row=document.getElementById('shl-row-'+r.project);
    if(row)row.classList.toggle('is-behind',behindOf(r.project)>0);
  }));
  if(rowKey!==key)return;
  const count=root.querySelector('#shl-count');
  if(count)count.textContent=summary(list);
  /* move the rows into their settled order without rebuilding them */
  const box=root.querySelector('.shl-rows');
  if(box)for(const r of rows()){
    const el=document.getElementById('shl-row-'+r.project);
    if(el)box.appendChild(el);
  }
}
/* Poll tick: the strip, inbox and per-row timestamps repaint from the new
   snapshot; only a change in WHICH projects are shared rebuilds the rows. */
function onStatus(){
  if(!root)return;
  const list=rows();
  if(list.map(r=>r.project).join('\n')!==rowKey){render();return;}
  const strip=root.querySelector('#prj-sharing-strip');
  if(strip)strip.outerHTML=sharingStripHTML();
  const inbox=root.querySelector('#prj-shared');
  const html=sharedWithYouHTML();
  if(inbox)inbox.outerHTML=html;
  else{const b=root.querySelector('.prj-body');if(b&&html)b.insertAdjacentHTML('afterbegin',html);}
  for(const r of list){
    const row=document.getElementById('shl-row-'+r.project);
    const when=row&&row.querySelector('.shl-when');
    if(when)when.outerHTML=whenCell(r);
  }
  const count=root.querySelector('#shl-count');
  if(count)count.textContent=summary(list);
  bindSharingCopies(root);
  bindSharingActions(root);
}
