/* Projects catalog and on-demand file browsing.
   Catalog, file index and activity feeds load independently. Row and drawer
   nodes survive filtering/sorting; explicit refresh owns data invalidation. */
import {projectPage} from '../core/navigation.js?v=20260915-data1';
import {dataViewControls} from '../core/data-views.js?v=20260915-data1';
import {isProjectPinned,subscribeProjectPins} from '../core/project-pins.js?v=20260915-data1';
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

const filePath=id=>'/api/xo-projects/'+encodeURIComponent(id)+'/tree'
  +(cwd.get(id)?'?relative_path='+encodeURIComponent(cwd.get(id)):'');

let root=null,items=null,expanded=null;
let catalogDirty=false,catalogRevision=0,loading=false,lastLoaded=0,pollTimer=null;
let listError='';
let switchTo=()=>{},refreshToolbar=()=>{};
let counts=new Map(),live=new Map(),lastEvent=new Map();
const feeds={counts:'loading',activity:'loading',timeline:'loading'};
let filter='',viewFilter='all',sortK='activity',fdeb=null;
const SORTS=[['activity','Recent activity'],['name','Name'],['files','Indexed files'],['created','Newest created']];
const FILTERS=[['all','All projects'],['live','Live'],['pinned','Pinned']];
const rowNodes=new Map(),drawers=new Map();
subscribeProjectPins(()=>renderRows());

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
  refresh:loadList,
  async mount(el,ctx){
    root=el;switchTo=ctx.switchTo;refreshToolbar=ctx.refreshToolbar||(()=>{});
    el.innerHTML='<div class="prj">'+head()+'<div id="prj-status" role="status"></div>'
      +'<div class="prj-body"><div class="prj-cols" aria-hidden="true"><div class="prj-cols-inner">'
      +'<span></span><span>Project</span><span>Status</span><span>Indexed files</span><span>Last active</span>'
      +'</div></div><div class="prj-rows"></div>'
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
    &&(viewFilter!=='live'||live.has(p.id))&&(viewFilter!=='pinned'||isProjectPinned(p.id)));
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
  return '<header class="prj-hero"><h1 class="prj-sr-only">List</h1><p id="prj-summary"><span id="prj-count">Loading projects…</span></p></header>'
    +'<div class="prj-head">'+dataViewControls('project-list')+'<span class="prj-spacer"></span>'
    +'<div class="prj-filter-control"><label class="prj-filter-label" for="prj-filter">Filter</label>'
    +'<select id="prj-filter">'+FILTERS.map(([key,label])=>'<option value="'+key+'"'+(key===viewFilter?' selected':'')+'>'+label+' (—)</option>').join('')+'</select></div>'
    +'<div class="prj-sort-control"><label class="prj-sort-label" for="prj-sort">Sort by</label>'
    +'<select id="prj-sort">'+SORTS.map(([key,label])=>'<option value="'+key+'"'+(key===sortK?' selected':'')+'>'+label+'</option>').join('')+'</select></div></div>';
}
function updateHead(){
  if(!root)return;
  const count=root.querySelector('#prj-count');if(count)count.textContent=summary(items?visible().length:undefined);
  const totals={all:items?.length,pinned:items?.filter(p=>isProjectPinned(p.id)).length,
    live:feeds.activity==='ready'?items?.filter(p=>live.has(p.id)).length:undefined};
  const select=root.querySelector('#prj-filter');
  if(select.value!==viewFilter)select.value=viewFilter;
  for(const [key,label] of FILTERS){
    const option=select.querySelector('option[value="'+key+'"]');
    const text=label+' ('+(totals[key]??'—')+')';
    if(option.textContent!==text)option.textContent=text;
  }
  const notes=[];
  if(listError)notes.push((items?'Could not refresh projects. Showing the last list. ':'')+listError);
  if(feeds.counts==='error')notes.push('File index unavailable.');
  if(feeds.activity==='error')notes.push('Live status unavailable.');
  if(feeds.timeline==='error')notes.push('Recent activity unavailable.');
  const status=root.querySelector('#prj-status');status.textContent=notes.join(' ');status.hidden=!notes.length;
}
function bindHead(){
  root.querySelector('#prj-sort').addEventListener('change',event=>{sortK=event.target.value;renderRows();});
  root.querySelector('#prj-filter').addEventListener('change',event=>{
    if(!FILTERS.some(([key])=>key===event.target.value))return;
    viewFilter=event.target.value;renderRows();
  });
  root.querySelector('.prj-empty').addEventListener('click',async event=>{
    if(event.target.closest('[data-clear-projects]')){filter='';viewFilter='all';refreshToolbar();renderRows();}
    if(event.target.closest('[data-manage-projects]'))switchTo('projects/manage');
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
  if(!items.length)return'<b>No projects yet</b><p>Clone a Git repository into this Space to get started.</p><div class="prj-empty-actions"><button type="button" data-manage-projects>Manage projects</button><button type="button" data-first-run>Getting started</button></div>';
  let title='No matching projects',message='Try a different search or clear the filters.';
  if(!filter.trim()&&viewFilter==='pinned'){title='Keep your frequent projects here';message='Pin projects in Manage to keep them here. Pins are saved in this browser.';}
  if(!filter.trim()&&viewFilter==='live'){title=feeds.activity==='loading'?'Checking live projects…':feeds.activity==='error'?'Live status is unavailable':'No projects are live';message=feeds.activity==='ready'?'Projects with open agent sessions appear here.':'You can still browse all projects.';}
  return'<b>'+esc(title)+'</b><p>'+esc(message)+'</p><div class="prj-empty-actions"><button type="button" data-clear-projects>Show all projects</button>'+(viewFilter==='pinned'?'<button type="button" data-manage-projects>Manage projects</button>':'')+'</div>';
}
function liveCell(p){
  const entry=live.get(p.id);
  if(!entry)return'<span class="prj-cell prj-live is-idle">'+(feeds.activity==='ready'?'—':'…')+'</span>';
  return'<span class="prj-cell prj-live" title="'+esc([...entry.agents].join(', '))+' · active '+esc(rel(entry.since))+'"><i></i>Live</span>';
}
function filesCell(p){
  const c=counts.get(p.id);
  if(!c||c.known===false)return'<span class="prj-cell prj-num is-none">'+(feeds.counts==='loading'?'…':'Not indexed')+'</span>';
  /* zero + per-project scan cap: the graph may simply not have mapped this
     project, so don't claim the folder is empty — the drawer lists it live */
  if(!c.files)return c.capped
    ?'<span class="prj-cell prj-num is-none" title="This project&#39;s scan hit its cap, so its files may not be fully mapped. Open the row — the drawer lists the folder live.">no files mapped</span>'
    :'<span class="prj-cell prj-num is-none">no files yet</span>';
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
    +'<button class="prj-row-head" type="button" data-id="'+esc(p.id)+'" aria-expanded="false" aria-controls="prj-drawer-'+esc(p.id)+'">'+rowContent(p)+'</button></div></div>';
}
function updateRow(node,p){
  const open=expanded===p.id,button=node.querySelector('.prj-row-head');
  node.classList.toggle('is-open',open);button.setAttribute('aria-expanded',String(open));
  const html=rowContent(p);if(button.innerHTML!==html)button.innerHTML=html;
}
function bindRow(node,id){
  node.querySelector('.prj-row-head').addEventListener('click',()=>toggle(id));
}
function toggle(id){
  expanded=expanded===id?null:id;renderRows();
  rowNodes.get(id)?.querySelector('.prj-row-head').focus({preventScroll:true});
}
function makeDrawer(id){
  const el=document.createElement('div');el.className='prj-drawer';el.id='prj-drawer-'+id;
  el.setAttribute('role','region');el.setAttribute('aria-labelledby','prj-files-title-'+id);
  el.innerHTML='<div class="prj-detail-head"><h2 class="prj-files-title" id="prj-files-title-'+esc(id)+'">Files</h2>'
    +'<button type="button" class="prj-detail-refresh">Refresh files</button></div>'
    +'<div class="prj-panel prj-panel-wide"><div class="prj-pbody" data-panel="files"></div></div>';
  const state={el,slot:{version:0,loaded:false,pending:null,path:null}};drawers.set(id,state);
  el.querySelector('.prj-detail-refresh').addEventListener('click',()=>fillDrawer(id,true));
  return state;
}
function fillDrawer(id,force=false){return fillFiles(id,{force});}
async function fillFiles(id,{force=false}={}){
  const state=drawers.get(id);if(!state)return;
  const slot=state.slot;
  if(!force&&(slot.loaded||slot.pending))return;
  const version=++slot.version,path=API_BASE+filePath(id),previous=slot.pending,previousPath=slot.path;
  const el=state.el.querySelector('[data-panel="files"]');
  const button=state.el.querySelector('.prj-detail-refresh');
  const current=()=>drawers.get(id)===state&&slot.version===version;
  slot.path=path;slot.loaded=false;
  el.setAttribute('aria-busy','true');button.disabled=true;
  if(!el.textContent||previousPath!==path){
    el.innerHTML=crumbs(id,cwd.get(id)||'')+'<div class="prj-note">Loading files…</div>';bindFiles(el,id);
  }
  const run=(async()=>{
    // A forced refresh of the same URL waits for the shared GET, then reads
    // again. Its old result cannot paint after this generation was requested.
    if(force&&previous&&previousPath===path){await previous;if(!current())return;}
    let res=await boundedRead(apiFetch(path));
    if(!current())return;
    if(!res.ok&&res.status===404&&cwd.get(id)){
      cwd.set(id,'');res=await boundedRead(apiFetch(API_BASE+filePath(id)));if(!current())return;
    }
    try{
      if(res.ok&&(!res.data||res.data.project_id!==id||!Array.isArray(res.data.dirs)||!Array.isArray(res.data.files)))res={ok:false,error:'The folder could not be read.'};
      el.innerHTML=res.ok?rTree(res.data):crumbs(id,cwd.get(id)||'')+panelFail(res);
      slot.loaded=true;bindFiles(el,id);
    }catch{el.innerHTML='<div class="prj-note">These files could not be read. Try refreshing.</div>';slot.loaded=true;}
    el.removeAttribute('aria-busy');button.disabled=false;
  })();
  slot.pending=run;
  try{await run;}finally{if(current())slot.pending=null;}
}
function bindFiles(el,id){
  el.querySelectorAll('[data-cd]').forEach(button=>button.addEventListener('click',()=>{
    cwd.set(id,button.dataset.cd);fillFiles(id,{force:true});
  }));
  el.querySelectorAll('[data-file]').forEach(button=>button.addEventListener('click',()=>dispatchEvent(new CustomEvent('space:preview-file',{
    detail:{project:button.dataset.project,path:button.dataset.file,name:button.querySelector('.fx-name').textContent}}))));
}
