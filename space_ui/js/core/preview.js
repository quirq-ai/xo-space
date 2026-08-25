/* File previewer — a side drawer that renders one file from a project.

   Lives in core/, not in a view, because three surfaces open it (the Tree
   lens, the Files explorer, the graph's detail panel) and views never import
   each other. They dispatch `space:preview-file` with {project, path, name}
   and this module owns everything after that.

   Rendering rules, in order of how much they matter:
     - markdown goes through core/markdown.js, which escapes before it
       transforms and emits only fixed attribute-free tags;
     - HTML from disk is NEVER injected into this document. It renders in an
       iframe with an empty sandbox: no scripts, no same-origin, no forms, no
       top-level navigation. A file in the workspace is not trusted content —
       an agent wrote it — and the app it would otherwise be running inside
       holds the user's session;
     - anything else renders as escaped source text.
   The Source toggle shows raw text for every kind, which is also the escape
   hatch when a render looks wrong. */
import {API_BASE,apiFetch} from './api.js';
import {mdToHtml} from './markdown.js';

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const bytes=n=>n==null?'':n<1024?n+' B'
  :n<1048576?(n/1024).toFixed(n<10240?1:0)+' KB':(n/1048576).toFixed(1)+' MB';
function rel(iso){
  if(!iso)return'';
  const s=(Date.now()-new Date(iso).getTime())/1000;
  if(!isFinite(s))return'';
  if(s<60)return'just now';
  if(s<3600)return Math.floor(s/60)+'m ago';
  if(s<86400)return Math.floor(s/3600)+'h ago';
  return Math.floor(s/86400)+'d ago';
}

let el=null,body=null;
let current=null;   /* {project,path,name} */
let data=null;      /* the loaded payload */
let source=false;   /* Source toggle */
let token=0;        /* race guard: only the newest request may paint */

export function initPreview(){
  el=document.getElementById('preview');
  if(!el)return;
  body=el.querySelector('#preview-body');
  el.addEventListener('click',onClick);
  addEventListener('space:preview-file',e=>open(e.detail||{}));
  /* The drawer belongs to the Files context. The three lenses (List, Graph,
     Tree) all report tab 'projects' — see registry.js — so switching between
     them keeps the file open; landing on any other tab leaves it behind, a
     file preview having nothing to say about Sessions or Secrets. */
  addEventListener('space:view',e=>{
    if(e.detail?.tab!=='projects'&&el.classList.contains('is-open'))close();
  });
  addEventListener('keydown',e=>{
    /* Escape closes the preview first; the graph's own Escape handling only
       gets it once nothing is being previewed. */
    if(e.key==='Escape'&&el.classList.contains('is-open')){e.stopPropagation();close();}
  },true);
}

async function open({project,path,name}){
  if(!el||!project||!path)return;
  current={project,path,name:name||path.split('/').pop()};
  data=null;source=false;
  const mine=++token;
  el.classList.add('is-open');
  render('<div class="pv-note">loading…</div>');
  const res=await apiFetch(API_BASE+'/api/xo-projects/'+encodeURIComponent(project)
    +'/file?relative_path='+encodeURIComponent(path));
  if(mine!==token)return; /* a newer file is on screen */
  if(!res.ok){
    render('<div class="pv-note">'+esc(
      res.offline?'xo-space is unreachable'
      :res.status===415?'No text preview for this file type.'
      :res.error||'Could not read this file.')+'</div>');
    return;
  }
  data=res.data;
  render();
}
function close(){
  el.classList.remove('is-open');
  current=null;data=null;token++;
  if(body)body.innerHTML='';
}

function render(placeholder){
  el.querySelector('#preview-name').textContent=current?current.name:'';
  el.querySelector('#preview-path').textContent=current
    ?current.project+'/'+current.path:'';
  const meta=el.querySelector('#preview-meta');
  const toggle=el.querySelector('#preview-source');
  if(placeholder||!data){
    meta.textContent='';
    toggle.hidden=true;
    body.innerHTML=placeholder||'';
    return;
  }
  meta.textContent=[data.kind,bytes(data.size_bytes),rel(data.modified_at),
    data.truncated?'truncated':''].filter(Boolean).join(' · ');
  toggle.hidden=false;
  toggle.textContent=source?'Rendered':'Source';
  body.innerHTML=source?sourceHTML(data)
    :data.kind==='markdown'?'<div class="pv-md">'+mdToHtml(data.content)+'</div>'
    :data.kind==='html'?frameHTML(data)
    :sourceHTML(data);
  if(data.truncated)body.insertAdjacentHTML('beforeend',
    '<div class="pv-note">Showing the first 256 KB of this file.</div>');
}
const sourceHTML=d=>'<pre class="pv-src">'+esc(d.content)+'</pre>';
/* sandbox="" is the whole point: an empty allow-list means no scripts and a
   unique opaque origin, so the document cannot reach this page, its storage,
   or the API it is served from. srcdoc keeps it out of the network entirely. */
const frameHTML=d=>'<iframe class="pv-frame" sandbox="" referrerpolicy="no-referrer" '
  +'title="'+esc(d.name)+' preview" srcdoc="'+esc(d.content)+'"></iframe>';

function onClick(e){
  if(e.target.closest('#preview-close')){close();return;}
  if(e.target.closest('#preview-source')){source=!source;render();return;}
  if(e.target.closest('#preview-graph')&&current){
    /* Explicit, never automatic: the point of the previewer is that opening a
       file does NOT move you somewhere else. Leaf ids in space.json are
       '<project>:<relative path>'; atlas parks the request if the graph has
       not booted yet, so dispatch before the route change. */
    dispatchEvent(new CustomEvent('space:focus-project',
      {detail:current.project+':'+current.path}));
    location.hash='#/graph';
    close();
  }
}
