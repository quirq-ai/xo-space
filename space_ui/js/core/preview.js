/* File previewer — a floating window that renders one file from a project.

   Lives in core/, not in a view, because three surfaces open it (the Tree
   lens, the Files explorer, the graph's detail panel) and views never import
   each other. They dispatch `space:preview-file` with {project, path, name}
   and this module owns everything after that.

   It floats over the stage rather than docking to an edge: the window is
   draggable by its header and resizable from its corner, and it keeps
   whatever place the user drags it to for the rest of the session — the view
   underneath never moves, which is the point of previewing.

   Rendering rules, in order of how much they matter:
     - markdown goes through core/markdown.js, which escapes before it
       transforms and emits only fixed attribute-free tags;
     - HTML from disk is NEVER injected into this document. It renders in a
       sandboxed iframe whose scripts may run but never reach this app: no
       allow-same-origin means an opaque origin — no cookies, no storage, no
       parent access, and the API's CORS allowlist refuses it. Scripts are
       allowed because real documents (reports that reveal sections on
       scroll, app index pages) are blank without them; a file in the
       workspace is still not trusted content — an agent wrote it — which is
       why the origin line, not the script line, is what protects the
       user's session;
     - anything else renders as escaped source text.
   The Source toggle shows raw text for every kind, which is also the escape
   hatch when a render looks wrong.

   The header's version picker is the file's git history (/file-history) as a
   dropdown: pick a commit and the SAME rendered pane shows the document as
   that commit left it (/file with commit=), so flipping between versions is
   flipping between fully rendered documents — the clearest way to see how a
   file evolved. "Current" returns to the working-tree file. Versions load
   through the identical render path and cache per commit while the file
   stays open; the picker only appears once history is known to exist. */
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

let el=null,body=null,picker=null;
let current=null;   /* {project,path,name} */
let data=null;      /* the payload on screen (current or a version) */
let headData=null;  /* the working-tree payload, to return to */
let versions=null;  /* /file-history items, or null */
let cache=null;     /* Map hash → version payload, per open file */
let source=false;   /* Source toggle */
let token=0;        /* race guard: only the newest request may paint */

export function initPreview(){
  el=document.getElementById('preview');
  if(!el)return;
  body=el.querySelector('#preview-body');
  picker=el.querySelector('#preview-version');
  el.addEventListener('click',onClick);
  picker.addEventListener('change',()=>pick(picker.value));
  initDrag();
  addEventListener('space:preview-file',e=>open(e.detail||{}));
  /* The window belongs to the Files context. The three lenses (List, Graph,
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

/* Drag by the header. The stylesheet anchors the window to the top-right by
   default; the first drag converts that to explicit left/top once, and from
   then on the coordinates are the source of truth. Clamped so the header can
   never leave the viewport — a window you cannot grab cannot be recovered. */
function initDrag(){
  const header=el.querySelector('header');
  header.addEventListener('pointerdown',e=>{
    if(e.button!==0||e.target.closest('button,select'))return;
    const r=el.getBoundingClientRect();
    const dx=e.clientX-r.left,dy=e.clientY-r.top;
    el.style.left=r.left+'px';el.style.top=r.top+'px';el.style.right='auto';
    el.classList.add('is-dragging');
    header.setPointerCapture(e.pointerId);
    const move=ev=>{
      el.style.left=Math.min(Math.max(ev.clientX-dx,64-el.offsetWidth),innerWidth-64)+'px';
      el.style.top=Math.min(Math.max(ev.clientY-dy,0),innerHeight-48)+'px';
    };
    const up=()=>{
      header.removeEventListener('pointermove',move);
      header.removeEventListener('pointerup',up);
      el.classList.remove('is-dragging');
    };
    header.addEventListener('pointermove',move);
    header.addEventListener('pointerup',up);
  });
}

async function open({project,path,name}){
  if(!el||!project||!path)return;
  current={project,path,name:name||path.split('/').pop()};
  data=headData=versions=null;cache=new Map();source=false;
  picker.hidden=true;picker.innerHTML='';
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
  data=headData=res.data;
  render();
  loadVersions(mine);
}
function close(){
  el.classList.remove('is-open');
  current=null;data=null;headData=null;versions=null;cache=null;token++;
  picker.hidden=true;picker.innerHTML='';
  if(body){body.classList.remove('is-frame');body.innerHTML='';}
}

/* The version list arrives after the document: the picker only appears once
   there is a history to pick from, so a file outside any repo (or with no
   commits yet) simply never grows the control. */
async function loadVersions(mine){
  const res=await apiFetch(API_BASE+'/api/xo-projects/'+encodeURIComponent(current.project)
    +'/file-history?relative_path='+encodeURIComponent(current.path));
  if(mine!==token||!res.ok)return;
  const items=res.data?.is_repo?res.data.items:[];
  if(!items.length)return;
  versions=items;
  /* The option value is the item's index: hash and historical path travel
     together, and renames need both. */
  picker.innerHTML='<option value="">Current version</option>'
    +items.map((c,i)=>'<option value="'+i+'">'
      +esc(c.short_hash.slice(0,7)+' · '+(rel(c.date)||'')+' · '
        +(c.subject.length>44?c.subject.slice(0,43)+'…':c.subject))
      +'</option>').join('');
  picker.hidden=false;
}

/* Show one picked version — through the exact same render path as the live
   file, so a version IS the document as that commit left it. */
async function pick(value){
  if(!current)return;
  if(value===''){data=headData;render();return;}
  const item=versions?.[+value];
  if(!item)return;
  const mine=token,mycache=cache;
  const hit=mycache.get(item.hash);
  if(hit){data=hit;render();return;}
  render('<div class="pv-note">loading version '+esc(item.short_hash)+'…</div>');
  const res=await apiFetch(API_BASE+'/api/xo-projects/'+encodeURIComponent(current.project)
    +'/file?relative_path='+encodeURIComponent(current.path)
    +'&commit='+encodeURIComponent(item.hash)
    +(item.path?'&commit_path='+encodeURIComponent(item.path):''));
  if(mine!==token||picker.value!==value)return; /* moved on while loading */
  if(!res.ok){
    render('<div class="pv-note">'+esc(
      res.offline?'xo-space is unreachable'
      :res.error||'Could not load this version.')+'</div>');
    return;
  }
  mycache.set(item.hash,res.data);
  data=res.data;
  render();
}

/* The picked version's provenance, for the meta line. */
function versionMeta(){
  const item=versions?.[+picker.value];
  return item?['version '+item.short_hash.slice(0,7),rel(item.date)]:[];
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
    body.classList.remove('is-frame');
    body.innerHTML=placeholder||'';
    return;
  }
  meta.textContent=[...(picker.value!==''?versionMeta():[]),
    data.kind,bytes(data.size_bytes),rel(data.modified_at),
    data.truncated?'truncated':''].filter(Boolean).join(' · ');
  toggle.hidden=false;
  toggle.textContent=source?'Rendered':'Source';
  /* An HTML document gets the whole pane, edge to edge, and its own
     scrollbar — one scroll surface, the document's, not two nested ones. */
  const framed=!source&&data.kind==='html';
  body.classList.toggle('is-frame',framed);
  body.innerHTML=source?sourceHTML(data)
    :data.kind==='markdown'?'<div class="pv-md">'+mdToHtml(data.content)+'</div>'
    :data.kind==='html'?frameHTML(data)
    :sourceHTML(data);
  /* In frame mode the pane does not scroll, so the note would be invisible
     behind the iframe; the meta line's 'truncated' flag carries it there. */
  if(data.truncated&&!framed)body.insertAdjacentHTML('beforeend',
    '<div class="pv-note">Showing the first 256 KB of this file.</div>');
}
const sourceHTML=d=>'<pre class="pv-src">'+esc(d.content)+'</pre>';
/* The sandbox line that matters is the one NOT granted: no
   allow-same-origin, so the document runs in an opaque origin — no cookies,
   no storage, no parent window, and the API's CORS allowlist turns it away.
   allow-scripts is granted because scroll-reveal reports and app index
   pages are blank without it, and scripts cut off from every origin can
   only compute; the popup pair lets the document's ordinary target=_blank
   links open (a click is still required by popup blocking). No forms, no
   top-level navigation. */
const frameHTML=d=>'<iframe class="pv-frame" '
  +'sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox" '
  +'referrerpolicy="no-referrer" '
  +'title="'+esc(d.name)+' preview" srcdoc="'+esc(d.content)+'"></iframe>';

function onClick(e){
  if(e.target.closest('#preview-close')){close();return;}
  if(e.target.closest('#preview-source')){source=!source;render();}
}
