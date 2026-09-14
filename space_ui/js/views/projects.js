/* Projects catalog and on-demand Files, Activity and Issues details.
   Catalog, file index and activity feeds load independently. Row and drawer
   nodes survive filtering/sorting; explicit refresh owns data invalidation. */
import {projectPage} from '../core/navigation.js?v=20260914-navigation1';
import {API_BASE,apiFetch} from '../core/api.js';
import {workspaceCounts} from '../core/workspace.js?v=20260914-projectux1';

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
  const clearFilter=!visible().some(p=>p.id===id);
  if(clearFilter){filter='';viewFilter='all';clearTimeout(fdeb);refreshToolbar();}
  if(expanded!==id||clearFilter){expanded=id;render();}
  const row=document.getElementById('prj-row-'+id);
  if(row){
    row.scrollIntoView({block:'start',behavior:'smooth'});
    row.querySelector('.prj-row-head').focus({preventScroll:true});
  }
}

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const dtfmt=iso=>iso&&Number.isFinite(Date.parse(iso))?new Date(iso).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}):'—';
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
    fillPanel(id,PANELS.find(pn=>pn.key==='issues'),{force:true});
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
let catalogDirty=false,catalogRevision=0,loading=false,lastLoaded=0,pollTimer=null;
let listError='',pinNotice='';
let switchTo=()=>{},refreshToolbar=()=>{};
let counts=new Map(),live=new Map(),lastEvent=new Map();
const feeds={counts:'loading',activity:'loading',timeline:'loading'};
let filter='',viewFilter='all',sortK='activity',fdeb=null;
const SORTS=[['activity','Recent activity'],['name','Name'],['files','Indexed files'],['created','Newest created']];
const FILTERS=[['all','All projects'],['live','Live'],['pinned','Pinned']];
const GROUPS=[{key:'files',label:'Files',panels:['files']},
  {key:'activity',label:'Activity',panels:['todos','activity','timeline']},
  {key:'issues',label:'Issues',panels:['issues']}];
const rowNodes=new Map(),drawers=new Map();
const pinKey='space.projects.pins.v1:'+String(API_BASE||location.origin||'local');
let pinned=new Set();
try{
  const saved=JSON.parse(localStorage.getItem(pinKey)||'[]');
  if(Array.isArray(saved))pinned=new Set(saved.filter(id=>typeof id==='string'&&id.length<=200).slice(0,1000));
}catch{/* Storage is optional; pins still work for this visit. */}

addEventListener('space:projects-changed',event=>{
  catalogDirty=true;catalogRevision++;
  const id=event.detail?.project_id;
  if(id){drawers.delete(id);rowNodes.get(id)?.remove();rowNodes.delete(id);}
  if(root?.classList.contains('is-active'))loadList();
});

export default {
  ...projectPage('project-list'),section:'projects',
  toolbar:{search:{
    placeholder:'Filter projects…',getValue:()=>filter,
    setValue(value){filter=String(value??'');clearTimeout(fdeb);fdeb=setTimeout(renderRows,140);},
  }},
  async mount(el,ctx){
    root=el;switchTo=ctx.switchTo;refreshToolbar=ctx.refreshToolbar||(()=>{});
    el.innerHTML='<div class="prj">'+head()+'<div id="prj-status" role="status"></div>'
      +'<div class="prj-body"><div class="prj-cols" aria-hidden="true"><div class="prj-cols-inner">'
      +'<span></span><span>Project</span><span>Status</span><span>Indexed files</span><span>Last active</span>'
      +'</div><span class="prj-cols-map"></span></div><div class="prj-rows"></div>'
      +'<div class="prj-empty" hidden></div></div></div>';
    bindHead();renderRows();
    await loadList();
  },
  show(){
    if(catalogDirty||(items&&Date.now()-lastLoaded>30000))loadList();
    clearInterval(pollTimer);pollTimer=setInterval(()=>loadList(),30000);
  },
  hide(){clearInterval(pollTimer);pollTimer=null;}
};

/* Optional telemetry never holds up the catalog. Bound waiting here without
   changing the shared fetch layer or cancelling another view's shared GET. */
function boundedRead(promise){
  return new Promise(resolve=>{
    const timer=setTimeout(()=>resolve({ok:false,error:'Request timed out. Try refreshing.'}),12000);
    Promise.resolve(promise).then(result=>{clearTimeout(timer);resolve(result);},()=>{
      clearTimeout(timer);resolve({ok:false,error:'Could not read project data.'});
    });
  });
}
function loadFeed(key,promise,revision,apply){
  feeds[key]='loading';
  boundedRead(promise).then(res=>{
    if(revision!==catalogRevision)return;
    try{if(res.ok)apply(res);feeds[key]=res.ok?'ready':'error';}
    catch{feeds[key]='error';}
    // A failed read cannot keep advertising an old session as live.
    if(key==='activity'&&feeds[key]==='error')live=new Map();
    if(items)renderRows();else updateHead();
  });
}
async function loadList(){
  const revision=++catalogRevision;
  loading=true;listError='';renderRows();
  const listRequest=boundedRead(apiFetch(API_BASE+'/api/xo-projects'));
  loadFeed('counts',workspaceCounts(),revision,res=>{counts=res.byProject||new Map();});
  loadFeed('activity',apiFetch(API_BASE+'/api/xo-projects/activity'),revision,res=>{
    const next=new Map();
    for(const session of res.data?.open_sessions||[]){
      const id=session.project_id||session.agent;if(!id)continue;
      const entry=next.get(id)||{agents:new Set(),since:null};
      entry.agents.add(session.runtime||session.agent||'agent');
      const time=session.last_activity_at||session.opened_at;
      if(Number.isFinite(Date.parse(time))&&(!entry.since||Date.parse(time)>Date.parse(entry.since)))entry.since=time;
      next.set(id,entry);
    }
    live=next;
  });
  loadFeed('timeline',apiFetch(API_BASE+'/api/xo-projects/timeline?limit=200'),revision,res=>{
    const next=new Map();
    for(const event of res.data?.events||[]){
      const id=event.project_id,time=Date.parse(event.ts);
      if(!id||!Number.isFinite(time))continue;
      if(!next.has(id)||time>Date.parse(next.get(id)))next.set(id,event.ts);
    }
    lastEvent=next;
  });
  const list=await listRequest;
  if(revision!==catalogRevision)return;
  loading=false;
  if(!list.ok||!Array.isArray(list.data?.items)||!list.data.items.every(p=>p&&typeof p.id==='string'&&p.id)){
    listError=list.ok?'The project list could not be read.':list.error||'Could not load projects.';
    renderRows();return;
  }
  items=list.data.items;catalogDirty=false;lastLoaded=Date.now();
  const ids=new Set(items.map(p=>p.id));
  for(const [id,node] of rowNodes)if(!ids.has(id)){node.remove();rowNodes.delete(id);drawers.delete(id);}
  if(expanded&&!items.some(p=>p.id===expanded))expanded=null;
  render();openPending();
}

const filesOf=id=>counts.get(id)?.known===false?null:counts.get(id)?.files??null;
const activityOf=p=>Date.parse(live.get(p.id)?.since||lastEvent.get(p.id))||0;
function visible(){
  const words=filter.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const rows=(items||[]).filter(p=>words.every(word=>[p.id,p.display_name,p.description].join(' ').toLowerCase().includes(word))
    &&(viewFilter!=='live'||live.has(p.id))&&(viewFilter!=='pinned'||pinned.has(p.id)));
  const name=(a,b)=>String(a.display_name||a.id).localeCompare(String(b.display_name||b.id))||a.id.localeCompare(b.id);
  const by={name,files:(a,b)=>(filesOf(b.id)??-1)-(filesOf(a.id)??-1),
    created:(a,b)=>(b.unscaffolded?0:Date.parse(b.created_at)||0)-(a.unscaffolded?0:Date.parse(a.created_at)||0),
    activity:(a,b)=>Number(live.has(b.id))-Number(live.has(a.id))||activityOf(b)-activityOf(a)};
  return rows.sort((a,b)=>(by[sortK]||by.activity)(a,b)||name(a,b));
}
function summary(shown){
  if(!items)return loading?'Loading projects…':'Your local workspace';
  const known=items.map(p=>counts.get(p.id)).filter(c=>c&&c.known!==false);
  const files=known.reduce((sum,c)=>sum+c.files,0);
  const partial=known.some(c=>c.capped)||known.length<items.length;
  return items.length+' '+(items.length===1?'project':'projects')
    +(feeds.activity==='ready'?' · '+items.filter(p=>live.has(p.id)).length+' live':'')
    +(known.length?' · '+files.toLocaleString()+(partial?'+':'')+' indexed files':'')
    +(shown!==undefined&&shown!==items.length?' · '+shown+' shown':'');
}
function head(){
  return '<header class="prj-hero"><h1 class="prj-sr-only">List</h1><p id="prj-summary"><span id="prj-count">Loading projects…</span></p>'
    +'<div class="prj-actions"><button type="button" class="setup-primary" id="prj-add">Add project</button>'
    +'<button type="button" class="sess-refresh" id="prj-refresh" title="Refresh projects and activity">↻ Refresh</button></div></header>'
    +'<div class="prj-head"><div class="prj-filters" role="group" aria-label="Filter projects">'
    +FILTERS.map(([key,label])=>'<button type="button" data-project-filter="'+key+'" aria-pressed="'+(key===viewFilter)+'">'+label+' <span>—</span></button>').join('')
    +'</div><span class="prj-spacer"></span><label class="prj-sort-label" for="prj-sort">Sort by</label>'
    +'<select id="prj-sort">'+SORTS.map(([key,label])=>'<option value="'+key+'"'+(key===sortK?' selected':'')+'>'+label+'</option>').join('')+'</select></div>';
}
function updateHead(){
  if(!root)return;
  const count=root.querySelector('#prj-count');if(count)count.textContent=summary(items?visible().length:undefined);
  const refresh=root.querySelector('#prj-refresh');if(refresh){refresh.disabled=loading;refresh.classList.toggle('is-busy',loading);}
  const totals={all:items?.length,pinned:items?.filter(p=>pinned.has(p.id)).length,
    live:feeds.activity==='ready'?items?.filter(p=>live.has(p.id)).length:undefined};
  root.querySelectorAll('[data-project-filter]').forEach(button=>{
    button.setAttribute('aria-pressed',String(button.dataset.projectFilter===viewFilter));
    button.querySelector('span').textContent=totals[button.dataset.projectFilter]??'—';
  });
  const notes=[];
  if(listError)notes.push((items?'Could not refresh projects. Showing the last list. ':'')+listError);
  if(feeds.counts==='error')notes.push('File index unavailable.');
  if(feeds.activity==='error')notes.push('Live status unavailable.');
  if(feeds.timeline==='error')notes.push('Recent activity unavailable.');
  if(pinNotice)notes.push(pinNotice);
  const status=root.querySelector('#prj-status');status.textContent=notes.join(' ');status.hidden=!notes.length;
}
function bindHead(){
  root.querySelector('#prj-refresh').addEventListener('click',loadList);
  root.querySelector('#prj-add').addEventListener('click',()=>switchTo('setup/projects'));
  root.querySelector('#prj-sort').addEventListener('change',event=>{sortK=event.target.value;renderRows();});
  root.querySelectorAll('[data-project-filter]').forEach(button=>button.addEventListener('click',()=>{
    viewFilter=button.dataset.projectFilter;renderRows();
  }));
  root.querySelector('.prj-empty').addEventListener('click',async event=>{
    if(event.target.closest('[data-clear-projects]')){filter='';viewFilter='all';refreshToolbar();renderRows();}
    if(event.target.closest('[data-add-project]'))switchTo('setup/projects');
    if(event.target.closest('[data-retry-projects]'))loadList();
    if(event.target.closest('[data-first-run]')){
      await switchTo('wiki');
      if(location.hash==='#/wiki')dispatchEvent(new CustomEvent('space:wiki-page',{detail:'first-run'}));
    }
  });
}
function render(){renderRows();}
/* Keep row and drawer nodes. Sort/filter changes never refetch details or
   replace controls the user is editing. Moving a row retains focus/scroll. */
function renderRows(){
  if(!root)return;
  const container=root.querySelector('.prj-rows'),empty=root.querySelector('.prj-empty');
  const focused=root.contains(document.activeElement)?document.activeElement:null;
  const scrolls=[...root.querySelectorAll('.fx-pane')].map(el=>[el,el.scrollTop,el.scrollLeft]);
  const rows=visible(),shown=new Set(rows.map(p=>p.id));
  if(!items){
    container.innerHTML=loading?'<div class="prj-skel"></div>'.repeat(4):'';
    empty.innerHTML=listError?'<b>Projects could not load</b><p>'+esc(listError)+'</p><div class="prj-empty-actions"><button type="button" data-retry-projects>Try again</button></div>':'';
    empty.hidden=!listError;root.querySelector('.prj-cols').hidden=true;updateHead();return;
  }
  for(const skeleton of container.querySelectorAll(':scope > .prj-skel'))skeleton.remove();
  for(const p of items){
    let node=rowNodes.get(p.id);
    if(!node){const template=document.createElement('template');template.innerHTML=rowHTML(p);node=template.content.firstElementChild;rowNodes.set(p.id,node);bindRow(node,p.id);container.appendChild(node);}
    updateRow(node,p);
    node.hidden=!shown.has(p.id);
    const drawer=drawers.get(p.id);
    if(expanded===p.id){
      const state=drawer||makeDrawer(p.id);
      if(state.el.parentElement!==node)node.appendChild(state.el);
      if(shown.has(p.id))fillDrawer(p.id);
    }else drawer?.el.remove();
  }
  let position=container.firstElementChild;
  for(const p of rows){const node=rowNodes.get(p.id);if(node!==position)container.insertBefore(node,position);position=node.nextElementSibling;}
  empty.hidden=rows.length>0;root.querySelector('.prj-cols').hidden=!rows.length;
  if(!rows.length)empty.innerHTML=emptyHTML();
  for(const [el,top,left] of scrolls){el.scrollTop=top;el.scrollLeft=left;}
  if(focused?.isConnected&&focused.getClientRects().length)focused.focus({preventScroll:true});
  updateHead();
}
function emptyHTML(){
  if(!items.length)return'<b>No projects yet</b><p>Clone a Git repository into this Space to get started.</p><div class="prj-empty-actions"><button type="button" data-add-project>Add project</button><button type="button" data-first-run>Getting started</button></div>';
  let title='No matching projects',message='Try a different search or clear the filters.';
  if(!filter.trim()&&viewFilter==='pinned'){title='Keep your frequent projects here';message='Use the pin beside a project to add it to this list. Pins are saved in this browser.';}
  if(!filter.trim()&&viewFilter==='live'){title=feeds.activity==='loading'?'Checking live projects…':feeds.activity==='error'?'Live status is unavailable':'No projects are live';message=feeds.activity==='ready'?'Projects with open agent sessions appear here.':'You can still browse all projects.';}
  return'<b>'+esc(title)+'</b><p>'+esc(message)+'</p><div class="prj-empty-actions"><button type="button" data-clear-projects>Show all projects</button></div>';
}
function liveCell(p){
  const entry=live.get(p.id);
  if(!entry)return'<span class="prj-cell prj-live is-idle">'+(feeds.activity==='ready'?'—':'…')+'</span>';
  return'<span class="prj-cell prj-live" title="'+esc([...entry.agents].join(', '))+' · active '+esc(rel(entry.since))+'"><i></i>Live</span>';
}
function filesCell(p){
  const c=counts.get(p.id);
  if(!c||c.known===false)return'<span class="prj-cell prj-num is-none">'+(feeds.counts==='loading'?'…':'Not indexed')+'</span>';
  return'<span class="prj-cell prj-num" title="Indexed file count'+(c.capped?' (partial)':'')+'">'+c.files.toLocaleString()+(c.capped?'+':'')+' '+(c.files===1?'file':'files')
    +(c.folders?'<em>'+c.folders.toLocaleString()+' '+(c.folders===1?'folder':'folders')+'</em>':'')+'</span>';
}
function whenCell(p){
  const ts=live.get(p.id)?.since||lastEvent.get(p.id);
  if(ts)return'<span class="prj-cell prj-when" title="'+esc(dtfmt(ts))+'">'+esc(rel(ts))+'</span>';
  return'<span class="prj-cell prj-when is-none" title="No activity in the latest workspace events">'+(feeds.timeline==='loading'?'…':feeds.timeline==='error'?'Unavailable':'Not recorded')+'</span>';
}
function rowContent(p){
  return'<span class="caret" aria-hidden="true">'+(expanded===p.id?'▾':'▸')+'</span><span class="prj-cell prj-name"><b>'+esc(p.display_name||p.id)+'</b>'
    +(p.id!==p.display_name?'<em>'+esc(p.id)+'</em>':'')
    +(p.unscaffolded?'<span class="tchip" title="Folder without XO project metadata">Folder</span>':'')
    +(p.description?'<small>'+esc(p.description)+'</small>':'')+'</span>'+liveCell(p)+filesCell(p)+whenCell(p);
}
function rowHTML(p){
  return'<div class="prj-row" id="prj-row-'+esc(p.id)+'"><div class="prj-line">'
    +'<button class="prj-row-head" type="button" data-id="'+esc(p.id)+'" aria-expanded="false" aria-controls="prj-drawer-'+esc(p.id)+'">'+rowContent(p)+'</button>'
    +'<div class="prj-row-actions"><button class="prj-pin" type="button" aria-pressed="false">☆</button>'
    +'<button class="prj-map" type="button" data-map="'+esc(p.id)+'" title="Focus '+esc(p.display_name||p.id)+' on the graph">Graph</button></div></div></div>';
}
function updateRow(node,p){
  const open=expanded===p.id,button=node.querySelector('.prj-row-head');
  node.classList.toggle('is-open',open);button.setAttribute('aria-expanded',String(open));
  const html=rowContent(p);if(button.innerHTML!==html)button.innerHTML=html;
  const pin=node.querySelector('.prj-pin'),isPinned=pinned.has(p.id);
  pin.textContent=isPinned?'★':'☆';pin.setAttribute('aria-pressed',String(isPinned));
  const label=(isPinned?'Unpin ':'Pin ')+(p.display_name||p.id);
  pin.setAttribute('aria-label',label);pin.title=label;
}
function bindRow(node,id){
  node.querySelector('.prj-row-head').addEventListener('click',()=>toggle(id));
  node.querySelector('.prj-pin').addEventListener('click',()=>{
    if(pinned.has(id))pinned.delete(id);else pinned.add(id);
    try{localStorage.setItem(pinKey,JSON.stringify([...pinned]));pinNotice='';}
    catch{pinNotice='Pins are kept for this visit because browser storage is unavailable.';}
    renderRows();
    if(node.hidden)root.querySelector('[data-project-filter="pinned"]').focus({preventScroll:true});
  });
  node.querySelector('.prj-map').addEventListener('click',()=>{
    switchTo('graph');dispatchEvent(new CustomEvent('space:focus-project',{detail:id}));
  });
}
function toggle(id){
  expanded=expanded===id?null:id;renderRows();
  rowNodes.get(id)?.querySelector('.prj-row-head').focus({preventScroll:true});
}
function makeDrawer(id){
  const el=document.createElement('div');el.className='prj-drawer';el.id='prj-drawer-'+id;
  el.innerHTML='<div class="prj-detail-head"><nav class="prj-detail-tabs" role="tablist" aria-label="Project details">'
    +GROUPS.map(group=>'<button type="button" role="tab" data-project-tab="'+group.key+'" id="prj-tab-'+esc(id)+'-'+group.key+'" aria-controls="prj-detail-'+esc(id)+'-'+group.key+'">'+group.label+'</button>').join('')
    +'</nav><button type="button" class="prj-detail-refresh">Refresh details</button></div>'
    +GROUPS.map(group=>'<div class="prj-detail-group prj-panels" role="tabpanel" id="prj-detail-'+esc(id)+'-'+group.key+'" aria-labelledby="prj-tab-'+esc(id)+'-'+group.key+'" data-project-group="'+group.key+'">'
      +group.panels.map(key=>{const panel=PANELS.find(p=>p.key===key);return'<section class="prj-panel'+(panel.wide?' prj-panel-wide':'')+'"><h3 class="prj-ptitle">'+panel.title+'</h3><div class="prj-pbody" id="prjp-'+key+'" data-panel="'+key+'"></div></section>';}).join('')+'</div>').join('');
  const state={el,tab:'files',slots:new Map()};drawers.set(id,state);
  el.querySelectorAll('[data-project-tab]').forEach((button,index)=>{
    button.addEventListener('click',()=>selectDetail(id,button.dataset.projectTab));
    button.addEventListener('keydown',event=>{
      let next=null;if(event.key==='ArrowRight')next=(index+1)%GROUPS.length;if(event.key==='ArrowLeft')next=(index+GROUPS.length-1)%GROUPS.length;
      if(event.key==='Home')next=0;if(event.key==='End')next=GROUPS.length-1;if(next===null)return;
      event.preventDefault();selectDetail(id,GROUPS[next].key);el.querySelectorAll('[data-project-tab]')[next].focus();
    });
  });
  el.querySelector('.prj-detail-refresh').addEventListener('click',()=>fillDrawer(id,true));
  selectDetail(id,'files',false);return state;
}
function selectDetail(id,key,load=true){
  const state=drawers.get(id);if(!state||!GROUPS.some(g=>g.key===key))return;
  state.tab=key;
  state.el.querySelectorAll('[data-project-tab]').forEach(button=>{const active=button.dataset.projectTab===key;button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1;});
  state.el.querySelectorAll('[data-project-group]').forEach(group=>{group.hidden=group.dataset.projectGroup!==key;});
  if(load)fillDrawer(id);
}
function fillDrawer(id,force=false){
  const state=drawers.get(id);if(!state)return;
  for(const key of GROUPS.find(group=>group.key===state.tab).panels){
    if(force&&key==='issues')issRefresh.add(id);
    fillPanel(id,PANELS.find(panel=>panel.key===key),{force});
  }
}
async function fillPanel(id,pn,{force=false}={}){
  const state=drawers.get(id);if(!state)return;
  let slot=state.slots.get(pn.key);
  if(!slot){slot={version:0,loaded:false,pending:null,path:null};state.slots.set(pn.key,slot);}
  if(!force&&(slot.loaded||slot.pending))return;
  const version=++slot.version,path=API_BASE+pn.path(id),previous=slot.pending,previousPath=slot.path;
  const el=state.el.querySelector('[data-panel="'+pn.key+'"]');
  const current=()=>drawers.get(id)===state&&slot.version===version;
  slot.path=path;slot.loaded=false;
  el.setAttribute('aria-busy','true');
  if(pn.key==='files'&&(!el.textContent||previousPath!==path)){
    el.innerHTML=crumbs(id,cwd.get(id)||'')+'<div class="prj-note">Loading files…</div>';bindFiles(el,id,pn);
  }else if(!el.textContent)el.innerHTML='<div class="prj-skel is-sm"></div>'.repeat(pn.skel||1);
  const run=(async()=>{
    // A forced refresh of the same URL waits for the shared GET, then reads
    // again. Its old result cannot paint after this generation was requested.
    if(force&&previous&&previousPath===path){await previous;if(!current())return;}
    let res=await boundedRead(apiFetch(path));
    if(!current())return;
    if(!res.ok&&pn.key==='files'&&res.status===404&&cwd.get(id)){
      cwd.set(id,'');res=await boundedRead(apiFetch(API_BASE+pn.path(id)));if(!current())return;
    }
    try{
      if(res.ok&&pn.key==='files'&&(!res.data||res.data.project_id!==id||!Array.isArray(res.data.dirs)||!Array.isArray(res.data.files)))res={ok:false,error:'The folder could not be read.'};
      el.innerHTML=res.ok?pn.render(res.data):(pn.key==='files'?crumbs(id,cwd.get(id)||''):'')+panelFail(res);
      slot.loaded=true;
      if(res.ok&&pn.bind)pn.bind(el,id);
      if(pn.key==='files')bindFiles(el,id,pn);
    }catch{el.innerHTML='<div class="prj-note">These details could not be read. Try refreshing.</div>';slot.loaded=true;}
    el.removeAttribute('aria-busy');
  })();
  slot.pending=run;
  try{await run;}finally{if(current())slot.pending=null;}
}
function bindFiles(el,id,pn){
  el.querySelectorAll('[data-cd]').forEach(button=>button.addEventListener('click',()=>{
    cwd.set(id,button.dataset.cd);fillPanel(id,pn,{force:true});
  }));
  el.querySelectorAll('[data-file]').forEach(button=>button.addEventListener('click',()=>dispatchEvent(new CustomEvent('space:preview-file',{
    detail:{project:button.dataset.project,path:button.dataset.file,name:button.querySelector('.fx-name').textContent}}))));
}
