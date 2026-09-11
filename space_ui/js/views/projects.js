/* Projects tab — xo-projects observability (design direction 2026-07-15).
   Read-only v1: the project list (GET /api/xo-projects) with a per-project
   drawer showing the live todo board (.xo/todos.json via the watcher), open
   sessions, the recent timeline, and the project's GitHub issues (the
   poller's mirror). Every drawer panel is its own fetch — one dead source
   degrades one panel. Writes (todos CRUD, backup/restore, adopting an issue
   as a workitem) are deliberately not wired yet; the sync-vs-git decision is
   open. */
import {API_BASE,apiFetch} from '../core/api.js';
import {workspaceCounts} from '../core/workspace.js';

/* The Sharing lens hands off here: "open this project's drawer". The
   request is parked until the catalog is loaded, the same way the Graph
   parks space:focus-project until it has booted. */
let pendingOpen=null;
addEventListener('space:open-project',e=>{
  pendingOpen=String(e.detail||'');
  if(items)openPending();
});
function openPending(){
  if(!pendingOpen||!items)return;
  const id=pendingOpen;
  pendingOpen=null;
  if(!items.some(p=>p.id===id))return;
  /* a filter that hides the row would make the jump land on nothing */
  if(filter&&!visible().some(p=>p.id===id))filter='';
  if(expanded!==id){expanded=id;render();}
  const row=document.getElementById('prj-row-'+id);
  if(row){
    row.scrollIntoView({block:'start',behavior:'smooth'});
    row.querySelector('.prj-row-head').focus({preventScroll:true});
  }
}

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const dtfmt=iso=>iso?new Date(iso).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}):'—';
function rel(iso){
  if(!iso)return'—';
  const s=(Date.now()-new Date(iso).getTime())/1000;
  if(!isFinite(s))return'—';
  if(s<60)return'just now';
  if(s<3600)return Math.floor(s/60)+'m ago';
  if(s<86400)return Math.floor(s/3600)+'h ago';
  if(s<86400*30)return Math.floor(s/86400)+'d ago';
  return new Date(iso).toLocaleDateString(undefined,{dateStyle:'medium'});
}
function panelFail(res){
  if(res.notImplemented)return'<div class="prj-note">not available for the active agent</div>';
  if(res.offline)return'<div class="prj-note">xo-space is unreachable</div>';
  return'<div class="prj-note">'+esc(res.error)+'</div>';
}

/* status display order + chip class per todo status. The status vocabulary
   itself is defined once, in Python — services/cowork_agent/visualizer/
   todo_status.py — and tests/test_todo_status.py fails if the keys below stop
   matching it. Only the ORDER is a UI decision (in_progress first). */
const ST_ORDER={in_progress:0,pending:1,blocked:2,completed:3,cancelled:4};
const stChip=st=>'<span class="tchip st-'+esc(st)+'">'+esc(st.replace('_',' '))+'</span>';

function rTodos(d){
  const rows=[];
  for(const [sid,sess] of Object.entries(d.sessions||{})){
    for(const t of sess.todos||[])rows.push({t,runtime:sess.runtime||'',sid});
  }
  if(!rows.length)return'<div class="prj-note">no todos recorded yet</div>';
  rows.sort((a,b)=>(ST_ORDER[a.t.status]??9)-(ST_ORDER[b.t.status]??9));
  const shown=rows.slice(0,30);
  return'<div class="prj-todos">'
    +shown.map(({t,runtime})=>'<div class="prj-todo">'+stChip(t.status)
      +'<span class="tcontent'+(t.status==='completed'||t.status==='cancelled'?' done':'')+'">'+esc(t.content)+'</span>'
      +(runtime?'<span class="truntime">'+esc(runtime)+'</span>':'')+'</div>').join('')
    +(rows.length>shown.length?'<div class="prj-note">+'+(rows.length-shown.length)+' more</div>':'')
    +'</div>';
}
function rActivity(d){
  const ss=d.open_sessions||[];
  if(!ss.length)return'<div class="prj-note">no open sessions</div>';
  return'<div class="prj-list">'+ss.map(s=>'<div class="prj-li">'
    +'<b>'+esc(s.agent)+'</b>'+(s.runtime?' <span class="truntime">'+esc(s.runtime)+'</span>':'')
    +'<span class="tmuted">opened '+rel(s.opened_at)+' · active '+rel(s.last_activity_at)+'</span>'
    +'</div>').join('')+'</div>';
}
function rTimeline(d){
  const evs=d.events||[];
  if(!evs.length)return'<div class="prj-note">no events yet (the watcher hasn’t emitted any for this project)</div>';
  return'<div class="prj-list">'+evs.slice(0,20).map(e=>'<div class="prj-li">'
    +'<span class="tchip">'+esc(e.type)+'</span>'
    +(e.runtime?'<span class="truntime">'+esc(e.runtime)+'</span>':'')
    +'<span class="tmuted">'+rel(e.ts)+'</span>'
    +'</div>').join('')+'</div>';
}

/* ── file explorer ──────────────────────────────────────────────────────────
   One folder at a time from GET /api/xo-projects/{id}/tree?relative_path=…,
   which is bounded and path-safe server-side. Browsing state is per project
   (cwd) so reopening a drawer returns you to the folder you were in. */
const cwd=new Map(); /* projectId -> relative path, '' = project root */
const bytes=n=>{
  if(n==null)return'';
  if(n<1024)return n+' B';
  if(n<1024*1024)return (n/1024).toFixed(n<10240?1:0)+' KB';
  if(n<1024*1024*1024)return (n/1048576).toFixed(1)+' MB';
  return (n/1073741824).toFixed(1)+' GB';
};
function crumbs(id,rel){
  const parts=rel?rel.split('/'):[];
  const out=['<button class="fx-crumb" data-cd="" data-id="'+esc(id)+'">'+esc(id)+'</button>'];
  let acc='';
  parts.forEach((p,i)=>{
    acc=acc?acc+'/'+p:p;
    out.push('<span class="fx-sep">/</span>');
    out.push(i===parts.length-1
      ?'<b class="fx-here">'+esc(p)+'</b>'
      :'<button class="fx-crumb" data-cd="'+esc(acc)+'" data-id="'+esc(id)+'">'+esc(p)+'</button>');
  });
  return'<div class="fx-crumbs">'+out.join('')+'</div>';
}
/* Two panes: folders on the left (the thing you navigate with), files on the
   right (the thing you read). One list mixing both makes you hunt for the
   folder rows among fifty files every time you go a level deeper. */
function dirRow(id,e,up){
  return'<button class="fx-row is-dir'+(up?' is-up':'')+'" '
    +'data-cd="'+esc(e.relative_path)+'" data-id="'+esc(id)+'">'
    +'<span class="fx-ico">'+(up?'&#8629;':'&#9654;')+'</span>'
    +'<span class="fx-name">'+esc(e.name)+'</span>'
    +'<span class="fx-size">'+(up||e.entries==null?''
        :e.entries+' item'+(e.entries===1?'':'s'))+'</span>'
  +'</button>';
}
function fileRow(e,id){
  return'<button class="fx-row is-file" data-file="'+esc(e.relative_path)+'" '
    +'data-project="'+esc(id)+'">'
    +'<span class="fx-ico">&#183;</span>'
    +'<span class="fx-name" title="'+esc(e.name)+'">'+esc(e.name)+'</span>'
    +'<span class="fx-size">'+bytes(e.size_bytes)+'</span>'
    +'<span class="fx-when">'+rel2(e.modified_at)+'</span>'
  +'</button>';
}
function rTree(d){
  const id=d.project_id,rel=d.relative_path||'';
  cwd.set(id,rel); /* trust the server's answer over our optimistic guess */
  const dirs=[
    ...(rel?[{up:true,name:'..',relative_path:d.parent_relative_path||''}]:[]),
    ...d.dirs,
  ];
  if(!d.dirs.length&&!d.files.length&&!rel)
    return crumbs(id,rel)+'<div class="prj-note">this project has no files yet</div>';
  return crumbs(id,rel)
    +'<div class="fx-meta">'+d.dirs.length+' folder'+(d.dirs.length===1?'':'s')
      +' · '+d.files.length+' file'+(d.files.length===1?'':'s')+'</div>'
    +'<div class="fx-body'+(dirs.length?'':' is-files-only')+'">'
      +(dirs.length
        ?'<div class="fx-pane fx-dirs">'+dirs.map(e=>dirRow(id,e,e.up)).join('')+'</div>'
        :'')
      +'<div class="fx-pane fx-files">'
        +(d.files.length?d.files.map(f=>fileRow(f,id)).join('')
          :'<div class="prj-note">no files in this folder</div>')
      +'</div>'
    +'</div>';
}
const rel2=iso=>iso?rel(iso):'';

/* ── GitHub issues ──────────────────────────────────────────────────────────
   This panel reads the MIRROR, not GitHub: services/cowork_agent/
   github_poller.py polls every project with a github.com remote and writes
   ~/.quirq/projects/<id>/github/issues.json, and GET /github/issues serves
   that. So the list is exactly as fresh as the last poll — the header says
   when that was, and Refresh sends refresh=1, which makes the server poll
   now instead of waiting out the interval.

   State and search filter in the browser. One response carries every row the
   mirror holds, so a filter that refetched would be slower AND would spend
   GitHub budget to answer a question already on screen. Only Refresh costs a
   call. Filter state is kept per project so reopening a drawer returns you to
   the view you left, the way the file explorer's cwd does. */
const issues=new Map();     /* projectId -> {data, state:'open'|'closed'|'all', q} */
const issRefresh=new Set(); /* projects whose next fetch must re-poll GitHub */
const issView=id=>{
  if(!issues.has(id))issues.set(id,{data:null,state:'open',q:''});
  return issues.get(id);
};
const ISS_STATES=[['open','Open'],['closed','Closed'],['all','All']];
/* Which empty this is — the endpoint says so in `state`, and each one has a
   different next action, so none of them may render as the same grey line. */
const ISS_EMPTY={
  no_remote:'No github.com remote, so there is nothing to mirror. Point the '
    +'project’s origin at GitHub and the poller picks it up on its next tick.',
  never_polled:'Not polled yet. Refresh asks the server to check GitHub now.',
  issues_disabled:'Issues are turned off for this repository on GitHub.',
  empty:'No open issues.',
};
function issFail(d){
  if(d.state!=='error')return esc(ISS_EMPTY[d.state]||'No issues.');
  const e=d.error||{};
  return'Last poll failed: '+esc(e.message||e.kind||'unknown')
    +(e.at?' <span class="tmuted">'+esc(rel(e.at))+'</span>':'');
}
/* The note for "the fetch was fine, your filter matched nothing". Closed gets
   its own sentence: the poller only ever asks for OPEN issues, so a closed row
   exists only for an issue Space watched close — "none" here is not a claim
   that the repo has no closed issues. */
function issNoMatch(v){
  if(v.q.trim())return'Nothing matches “'+esc(v.q.trim())+'”.';
  if(v.state==='closed')return'No closed issues recorded. Space keeps an issue '
    +'once it watches it close; issues closed before it started watching stay on GitHub.';
  return'No open issues.';
}
const issCount=(d,state)=>state==='all'?d.issues.length
  :d.issues.filter(i=>i.state===state).length;

function rIssues(d){
  const id=d.project_id,v=issView(id);
  v.data=d;
  /* no remote is not a filterable list — it is a fact about the project */
  if(d.state==='no_remote')return'<div class="prj-note">'+issFail(d)+'</div>';
  return issHead(d,v)+'<div class="iss-list">'+issRows(id)+'</div>';
}
function issHead(d,v){
  const open=issCount(d,'open');
  return'<div class="iss-head">'
    +'<span class="iss-meta">'
      +(d.repo?'<b>'+esc(d.repo)+'</b>':'')
      +'<span>'+open+' open'+(d.tracked?' · '+d.tracked+' tracked':'')+'</span>'
      +(d.fetched_at?'<span class="tmuted">checked '+esc(rel(d.fetched_at))+'</span>'
        :'<span class="tmuted">never checked</span>')
    +'</span>'
    +'<span class="prj-spacer"></span>'
    +'<input class="tv-filter iss-q" type="search" placeholder="Filter issues…" '
      +'autocomplete="off" spellcheck="false" aria-label="Filter issues" '
      +'value="'+esc(v.q)+'">'
    +'<div class="prj-sort" role="group" aria-label="Issue state">'
      +ISS_STATES.map(([k,label])=>'<button type="button" data-iss-state="'+k+'"'
        +(v.state===k?' class="is-on" aria-pressed="true"':' aria-pressed="false"')
        +'>'+label+' '+issCount(d,k)+'</button>').join('')
    +'</div>'
    +'<button class="sess-refresh" data-iss-refresh type="button" '
      +'title="Ask the server to poll GitHub now">&#8635; Refresh</button>'
  +'</div>';
}
function issRows(id){
  const v=issues.get(id);
  if(!v||!v.data)return'';
  const d=v.data;
  if(d.state!=='ok')return'<div class="prj-note">'+issFail(d)+'</div>';
  const q=v.q.trim().toLowerCase();
  const rows=d.issues.filter(it=>
    (v.state==='all'||it.state===v.state)
    &&(!q
      ||String(it.title||'').toLowerCase().includes(q)
      ||('#'+it.number).includes(q)
      ||(it.labels||[]).some(l=>String(l).toLowerCase().includes(q))
      ||(it.assignees||[]).some(a=>String(a.login||'').toLowerCase().includes(q))));
  if(!rows.length)return'<div class="prj-note">'+issNoMatch(v)+'</div>';
  return rows.map(issRow).join('');
}
/* An issue row is a link out to GitHub — the one place in Space that leaves
   the app, because the thing you do next with an issue (read it, comment,
   close it) is not something a read-only map can offer. Titles and labels are
   GitHub text, escaped like everything else; the href is only rendered when
   it is really an https URL, so a bad mirror row degrades to plain text
   rather than becoming a javascript: link. */
function issRow(it){
  const link=/^https:\/\//i.test(String(it.url||''));
  const tag=link?'a':'div';
  const labels=(it.labels||[]).slice(0,3)
    .map(l=>'<span class="iss-label">'+esc(l)+'</span>').join('');
  /* raw here, escaped at each interpolation — escaping once at the source and
     again at a use site is how &amp;lt; ends up on screen */
  const who=(it.assignees||[]).map(a=>String(a.login||'')).join(', ');
  return'<'+tag+' class="iss-row'+(it.state==='closed'?' is-closed':'')+'"'
    +(link?' href="'+esc(it.url)+'" target="_blank" rel="noopener noreferrer"':'')
    +' title="'+esc((it.title||'')+(who?' — '+who:''))+'">'
    +'<span class="iss-dot" aria-hidden="true"></span>'
    +'<span class="iss-num">#'+esc(it.number||'?')+'</span>'
    +'<span class="iss-title">'+esc(it.title||'(untitled)')+'</span>'
    +'<span class="iss-chips">'+labels
      +(it.in_progress?'<span class="tchip st-in_progress">in progress</span>'
        :it.adopted?'<span class="tchip">tracked</span>':'')
      +(who?'<span class="truntime">'+esc(who)+'</span>':'')
    +'</span>'
    +'<span class="iss-when">'+esc(rel2(it.updated_at))+'</span>'
  +'</'+tag+'>';
}
let issDeb=null;
function bindIssues(el,id){
  const v=issues.get(id);
  if(!v)return;
  /* Repaint the LIST, never the head: rebuilding the head mid-keystroke
     destroys the filter input and throws the caret to the end (the same
     lesson renderRows() carries for the project list). */
  const repaint=()=>{
    const list=el.querySelector('.iss-list');
    if(list)list.innerHTML=issRows(id);
  };
  el.querySelectorAll('[data-iss-state]').forEach(b=>b.addEventListener('click',()=>{
    v.state=b.dataset.issState;
    el.querySelectorAll('[data-iss-state]').forEach(x=>{
      const on=x.dataset.issState===v.state;
      x.classList.toggle('is-on',on);
      x.setAttribute('aria-pressed',on?'true':'false');
    });
    repaint();
  }));
  const q=el.querySelector('.iss-q');
  if(q)q.addEventListener('input',e=>{
    v.q=e.target.value;
    clearTimeout(issDeb);
    issDeb=setTimeout(repaint,140);
  });
  const r=el.querySelector('[data-iss-refresh]');
  if(r)r.addEventListener('click',()=>{
    r.disabled=true;
    issRefresh.add(id);
    const list=el.querySelector('.iss-list');
    if(list)list.innerHTML='<div class="prj-note">asking GitHub…</div>';
    fillPanel(id,PANELS.find(pn=>pn.key==='issues'));
  });
}

const PANELS=[
  {key:'files',   title:'Files',        wide:true, skel:3,
                                        path:id=>'/api/xo-projects/'+encodeURIComponent(id)+'/tree'
                                              +(cwd.get(id)?'?relative_path='+encodeURIComponent(cwd.get(id)):''),
                                                                                                 render:rTree},
  {key:'todos',   title:'Todos',        path:id=>'/api/xo-projects/'+encodeURIComponent(id)+'/todos',            render:rTodos},
  {key:'activity',title:'Open sessions',path:id=>'/api/xo-projects/'+encodeURIComponent(id)+'/activity',         render:rActivity},
  {key:'timeline',title:'Recent events',path:id=>'/api/xo-projects/'+encodeURIComponent(id)+'/timeline?limit=20',render:rTimeline},
  /* Refresh is a one-shot flag rather than a path argument: the button sets
     it, the next fetch spends it, and every other fetch of this panel reads
     the mirror for free. */
  {key:'issues',  title:'Issues',       wide:true, skel:3,
                                        path:id=>{
                                          const force=issRefresh.has(id);
                                          issRefresh.delete(id);
                                          return'/api/xo-projects/'+encodeURIComponent(id)
                                            +'/github/issues'+(force?'?refresh=1':'');
                                        },
                                        render:rIssues, bind:bindIssues},
];

let root=null,items=null,expanded=null;
let switchTo=()=>{}; /* ctx.switchTo, captured on mount */
/* Workspace-wide rollups: four requests total, whatever the project count.
   Each degrades on its own — a dead timeline costs the "last active" column,
   not the list. */
let counts=new Map();      /* id -> {files,folders} from space.json */
let live=new Map();        /* id -> {agents:[…], since} from the workspace activity */
let lastEvent=new Map();   /* id -> ISO of the newest workspace event */
let capped=false;
let filter='',sortK='activity';
const SORTS=[['activity','Activity'],['name','Name'],['files','Files'],['created','Created']];

export default {
  /* The Files tab lands here, on the List lens; the Graph and Tree lenses are
     nav:false with parent:'projects' so this tab stays lit for all three. */
  id:'projects',label:'Files',order:1,
  async mount(el,ctx){
    root=el;
    switchTo=ctx.switchTo;
    el.innerHTML='<div class="prj">'+skeleton()+'</div>';
    await loadList();
  },
  show(){/* keep whatever the user had open; Refresh re-fetches */}
};

const skeleton=()=>'<div class="prj-head"></div><div class="prj-rows">'
  +'<div class="prj-skel"></div>'.repeat(4)+'</div>';

async function loadList(){
  const box=root.querySelector('.prj');
  const btn=root.querySelector('#prj-refresh');
  if(btn){btn.disabled=true;btn.classList.add('is-busy');}
  /* One barrier, deliberately: the row grid is a table and a table with
     columns arriving one by one reads as broken. Four requests, not 4×N. */
  const [list,ws,act,tl]=await Promise.all([
    apiFetch(API_BASE+'/api/xo-projects'),
    workspaceCounts(),
    apiFetch(API_BASE+'/api/xo-projects/activity'),
    apiFetch(API_BASE+'/api/xo-projects/timeline?limit=200'),
  ]);
  if(!list.ok){
    box.innerHTML=head(0)+panelFail(list);
    bindHead();
    return;
  }
  items=list.data.items||[];
  counts=ws.byProject||new Map();
  capped=!!ws.totalsCapped;
  live=new Map();
  if(act.ok)for(const s of act.data.open_sessions||[]){
    const id=s.project_id||s.agent;
    if(!id)continue;
    const e=live.get(id)||{agents:new Set(),since:null};
    e.agents.add(s.runtime||s.agent||'agent');
    const t=s.last_activity_at||s.opened_at;
    if(t&&(!e.since||t>e.since))e.since=t;
    live.set(id,e);
  }
  lastEvent=new Map();
  if(tl.ok)for(const ev of tl.data.events||[]){
    const id=ev.project_id;
    if(!id||!ev.ts)continue;
    if(!lastEvent.has(id)||ev.ts>lastEvent.get(id))lastEvent.set(id,ev.ts);
  }
  /* Refresh must not close what you were reading. */
  if(expanded&&!items.some(p=>p.id===expanded))expanded=null;
  render();
  openPending();
}

const filesOf=id=>counts.get(id)?.files??null;
const activityOf=p=>live.has(p.id)?'9999':(lastEvent.get(p.id)||p.created_at||'');
function visible(){
  const q=filter.trim().toLowerCase();
  const rows=items.filter(p=>!q
    ||p.id.toLowerCase().includes(q)
    ||String(p.display_name||'').toLowerCase().includes(q));
  const by={
    name:(a,b)=>String(a.display_name||a.id).localeCompare(String(b.display_name||b.id)),
    files:(a,b)=>(filesOf(b.id)??-1)-(filesOf(a.id)??-1),
    created:(a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||'')),
    activity:(a,b)=>String(activityOf(b)).localeCompare(String(activityOf(a))),
  };
  return rows.sort(by[sortK]||by.activity);
}

function summary(shown){
  const t={projects:items?items.length:0,
    files:[...counts.values()].reduce((s,c)=>s+c.files,0),
    folders:[...counts.values()].reduce((s,c)=>s+c.folders,0)};
  const un=(items||[]).filter(p=>p.unscaffolded).length;
  const n=(v,word)=>v.toLocaleString()+' '+word+(v===1?'':'s');
  return n(t.projects,'project')
    +(t.files?' · '+t.files.toLocaleString()+(capped?'+':'')+' file'+(t.files===1?'':'s')
      +' · '+n(t.folders,'folder'):'')
    +(un?' · '+un+' unscaffolded':'')
    +(shown!==undefined&&shown!==t.projects?' · '+shown+' shown':'');
}
function head(n){
  return'<div class="prj-head">'
    +'<span class="prj-eyebrow" id="prj-count">'+esc(summary(n))+'</span>'
    +'<span class="prj-spacer"></span>'
    +'<input class="tv-filter" id="prj-filter" placeholder="Filter projects…" '
      +'autocomplete="off" spellcheck="false" aria-label="Filter projects" '
      +'value="'+esc(filter)+'">'
    +'<div class="prj-sort" role="group" aria-label="Sort projects">'
      +SORTS.map(([k,label])=>'<button type="button" data-sort="'+k+'"'
        +(sortK===k?' class="is-on" aria-pressed="true"':' aria-pressed="false"')
        +'>'+label+'</button>').join('')
    +'</div>'
    +'<button class="sess-refresh" id="prj-refresh" title="Re-fetch the project list">'
      +'&#8635; Refresh</button>'
  +'</div>';
}

function render(){
  const rows=visible();
  root.querySelector('.prj').innerHTML=
    head(rows.length)
    +'<div class="prj-body">'+rowsHTML(rows)+'</div>';
  bindHead();
  bindRows();
  if(expanded)fillDrawer(expanded);
}
/* Repaint the rows only. Rebuilding the head would destroy the filter input
   mid-keystroke and throw the caret to the end — which is what the old
   focus/setSelectionRange hack was papering over. */
function renderRows(){
  const rows=visible();
  const box=root.querySelector('.prj-body');
  if(!box){render();return;}
  box.innerHTML=rowsHTML(rows);
  bindRows();
  const count=root.querySelector('#prj-count');
  if(count)count.textContent=summary(rows.length);
  if(expanded)fillDrawer(expanded);
}
function rowsHTML(rows){
  return(rows.length
      ?'<div class="prj-cols" aria-hidden="true">'
        +'<div class="prj-cols-inner"><span></span><span>Project</span><span></span>'
          +'<span>Files</span><span>Last active</span></div>'
        +'<span class="prj-cols-map"></span>'
      +'</div><div class="prj-rows">'+rows.map(rowHTML).join('')+'</div>'
      :'<div class="prj-empty">'
        +(items.length?'<b>No project matches “'+esc(filter)+'”</b>'
            +'<p>Clear the filter to see all '+items.length+'.</p>'
          :'<b>No projects in this workspace yet</b>'
            +'<p>A project is any folder directly under your workspace (the XO '
            +'root — see Setup). Clone or <code>mkdir</code> one there, ask your '
            +'coding agent to “create an xo-project”, or '
            +'<code>POST /api/files/mkdir</code> with <code>scaffold:true</code>; '
            +'it appears here on the watcher’s next tick.</p>'
            +'<p><button class="sess-refresh" data-first-run>Read: Your first run</button> '
            +'<button class="sess-refresh" data-open-setup>Open Setup</button></p>')
      +'</div>');
}
function bindRows(){
  root.querySelectorAll('.prj-row-head').forEach(h=>
    h.addEventListener('click',()=>toggle(h.dataset.id)));
  root.querySelectorAll('.prj-map').forEach(b=>b.addEventListener('click',()=>{
    switchTo('graph');
    dispatchEvent(new CustomEvent('space:focus-project',{detail:b.dataset.map}));
  }));
  /* empty-state hand-offs: the wiki listens for space:wiki-page once mounted,
     so switch first and select the page after (same order quirq.js uses) */
  const fr=root.querySelector('[data-first-run]');
  if(fr)fr.addEventListener('click',async()=>{
    await switchTo('wiki');
    dispatchEvent(new CustomEvent('space:wiki-page',{detail:'first-run'}));
  });
  const st=root.querySelector('[data-open-setup]');
  if(st)st.addEventListener('click',()=>switchTo('secrets'));
}
function syncSortUI(){
  root.querySelectorAll('[data-sort]').forEach(b=>{
    const on=b.dataset.sort===sortK;
    b.classList.toggle('is-on',on);
    b.setAttribute('aria-pressed',on?'true':'false');
  });
}
let fdeb=null;
function bindHead(){
  const r=root.querySelector('#prj-refresh');
  if(r)r.addEventListener('click',loadList);
  root.querySelectorAll('[data-sort]').forEach(b=>
    b.addEventListener('click',()=>{sortK=b.dataset.sort;syncSortUI();renderRows();}));
  const f=root.querySelector('#prj-filter');
  if(f)f.addEventListener('input',e=>{
    filter=e.target.value;
    clearTimeout(fdeb);
    fdeb=setTimeout(renderRows,140);
  });
}

function liveCell(p){
  const l=live.get(p.id);
  if(!l)return'<span class="prj-cell prj-live is-idle"></span>';
  const agents=[...l.agents].join(', ');
  return'<span class="prj-cell prj-live" title="'+esc(agents)+' · active '+esc(rel(l.since))+'">'
    +'<i></i>live</span>';
}
function filesCell(p){
  const c=counts.get(p.id);
  if(!c)return'<span class="prj-cell prj-num is-none">—</span>';
  /* zero + workspace cap: the graph may simply not have mapped this project,
     so don't claim the folder is empty — the drawer lists it live */
  if(!c.files)return capped
    ?'<span class="prj-cell prj-num is-none" title="The workspace graph is at its cap, so this project&#39;s files may not be mapped. Open the row — the drawer lists the folder live.">no files mapped</span>'
    :'<span class="prj-cell prj-num is-none">no files yet</span>';
  return'<span class="prj-cell prj-num">'
    +c.files.toLocaleString()+(c.capped?'+':'')+' file'+(c.files===1?'':'s')
    +(c.folders?'<em>'+c.folders.toLocaleString()+' folder'+(c.folders===1?'':'s')+'</em>':'')
    +'</span>';
}
function whenCell(p){
  const l=live.get(p.id);
  const ts=l?l.since:lastEvent.get(p.id);
  if(ts)return'<span class="prj-cell prj-when" title="'+esc(dtfmt(ts))+'">'+esc(rel(ts))+'</span>';
  return'<span class="prj-cell prj-when is-none" title="'+esc(dtfmt(p.created_at))+'">created '
    +esc(rel(p.created_at))+'</span>';
}

function rowHTML(p){
  const open=expanded===p.id;
  const empty=counts.get(p.id)&&!counts.get(p.id).files;
  return'<div class="prj-row'+(open?' is-open':'')+(empty?' is-empty':'')+'" '
      +'id="prj-row-'+esc(p.id)+'">'
    +'<div class="prj-line">'
      /* a real button: keyboard-reachable, and it says what it does to a
         screen reader. Map sits OUTSIDE it — a button inside a button is
         invalid markup and is why this used to need stopPropagation. */
      +'<button class="prj-row-head" type="button" data-id="'+esc(p.id)+'" '
        +'aria-expanded="'+(open?'true':'false')+'" '
        +'aria-controls="prj-drawer-'+esc(p.id)+'">'
        +'<span class="caret" aria-hidden="true">'+(open?'&#9662;':'&#9656;')+'</span>'
        +'<span class="prj-cell prj-name"><b>'+esc(p.display_name||p.id)+'</b>'
          +(p.id!==p.display_name?'<em>'+esc(p.id)+'</em>':'')
          +(p.unscaffolded?'<span class="tchip st-blocked">unscaffolded</span>':'')
          +(p.description?'<small>'+esc(p.description)+'</small>':'')
        +'</span>'
        +liveCell(p)
        +filesCell(p)
        +whenCell(p)
      +'</button>'
      +'<button class="prj-map" type="button" data-map="'+esc(p.id)+'" '
        +'title="Focus '+esc(p.display_name||p.id)+' on the graph">Map</button>'
    +'</div>'
    +(open?'<div class="prj-drawer" id="prj-drawer-'+esc(p.id)+'">'
      +'<div class="prj-panels">'+PANELS.map(pn=>
        '<div class="prj-panel'+(pn.wide?' prj-panel-wide':'')+'">'
        +'<div class="prj-ptitle">'+pn.title+'</div>'
        +'<div class="prj-pbody" id="prjp-'+pn.key+'">'
        +'<div class="prj-skel is-sm"></div>'.repeat(pn.skel||1)
        +'</div></div>').join('')
      +'</div></div>':'')
    +'</div>';
}
function toggle(id){
  expanded=expanded===id?null:id;
  render();
  if(expanded){
    const row=document.getElementById('prj-row-'+expanded);
    if(row)row.querySelector('.prj-row-head').focus({preventScroll:true});
  }
}
/* five independent fetches per drawer — no barrier, no shared failure */
function fillDrawer(id){
  for(const pn of PANELS)fillPanel(id,pn);
}
async function fillPanel(id,pn){
  let res=await apiFetch(API_BASE+pn.path(id));
  /* An agent can delete the folder you are standing in. cwd outlives the
     drawer, so without this the panel renders one grey sentence with no
     crumb and no "..", and reopening the row reproduces it forever. */
  if(!res.ok&&pn.key==='files'&&res.status===404&&cwd.get(id)){
    cwd.set(id,'');
    res=await apiFetch(API_BASE+pn.path(id));
  }
  if(expanded!==id)return; /* drawer changed while in flight */
  const el=document.getElementById('prjp-'+pn.key);
  if(!el)return;
  el.innerHTML=res.ok?pn.render(res.data)
    /* keep the breadcrumb outside the response so a failed fetch cannot
       take the way back with it */
    :(pn.key==='files'?crumbs(id,cwd.get(id)||''):'')+panelFail(res);
  /* panels with controls wire them here; a failed fetch renders no controls */
  if(res.ok&&typeof pn.bind==='function'){pn.bind(el,id);return;}
  if(pn.key!=='files')return;
  el.querySelectorAll('[data-cd]').forEach(b=>
    b.addEventListener('click',()=>{
      cwd.set(b.dataset.id,b.dataset.cd);
      el.innerHTML='<div class="prj-note">loading…</div>';
      fillPanel(b.dataset.id,pn);
    }));
  /* Opening a file previews it beside the list — the drawer stays open and
     the list keeps its position. */
  el.querySelectorAll('[data-file]').forEach(b=>
    b.addEventListener('click',()=>dispatchEvent(new CustomEvent('space:preview-file',{
      detail:{project:b.dataset.project,path:b.dataset.file,
        name:b.querySelector('.fx-name').textContent}}))));
}
