/* Tree — the third Files lens, beside List and Graph.

   Same data as the Graph (.xo/space.json: every project, every mapped
   folder, every mapped file), read as a hierarchy instead of as a force
   layout. The graph answers "what is near what"; this answers "what is
   in there", which is the question a file tree is actually good at.

   Shape: a horizontal tree. The workspace root sits on the left, each level
   of containers opens one column to the right, and the connectors are drawn
   as curves in an SVG layer under the node chips. Files do NOT get their own
   column — they stack vertically in one block beside the folder that holds
   them. A folder with 300 files would otherwise be 300 columns wide, and the
   sideways scroll would make the tree unreadable; stacking the leaves keeps
   horizontal distance meaning depth and nothing else.

   It reads the dataset directly rather than borrowing atlas.js's model:
   views never import each other (see the registry contract), the payload
   is cached server-side for 30s, and apiFetch single-flights concurrent
   GETs — so the second reader costs nothing. */
import {API_BASE,apiFetch} from '../core/api.js';

const plural=(n,word)=>n.toLocaleString()+' '+word+(n===1?'':'s');
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

/* Layout constants, all in px of the (unscaled) tree surface. */
const COL_W=212;   /* one level of depth */
const ROW_H=34;    /* a container chip */
const FILE_H=20;   /* a row in a leaf stack */
const STACK_PAD=13;/* the leaf card's own padding */
const GAP=7;       /* between sibling blocks */
/* The left gutter comes from the camera's initial x (it matches the centred
   header), so the surface itself starts at zero. */
const PAD_X=0,PAD_Y=24;
/* A folder's files show a dozen at a time. The old cap of 40 made one folder
   800px tall, which is what pushed its siblings into the far distance and
   left the big vertical voids. */
const FILES_SHOWN=12;

let root=null;
let go=()=>{};
let model=null;      /* the tree, or null before the first load */
let open=new Set();  /* expanded keys */
let filter='';
let loading=false;
const expandedStacks=new Set(); /* leaf cards showing all of their files */
let seen=new Set();             /* keys on screen last render — the rest are new */
let growing=false;              /* this render came from an expand: draw branches in */
let anchor=null;                /* {key,left,top} — keep this node where it was */

/* ── camera ───────────────────────────────────────────────────────────────
   The pane is not a scroller: the surface is a canvas you pan by dragging
   and zoom with the wheel, cursor-anchored — the same feel as the Graph
   lens next door. screen = layout*k + (x,y); k=1 is 1:1 px. The camera
   survives re-renders (the surface is re-created, the transform re-applied). */
let cam=null;                   /* {x,y,k}; null until the first render sizes it */
const K_MIN=.25,K_MAX=2.5;
let panning=false,panMoved=false,px=0,py=0,downX=0,downY=0;
const canvasEl=()=>root.querySelector('.tv-canvas');
function applyCam(){
  const s=root.querySelector('.tv-surface');
  if(s&&cam)s.style.transform='translate('+cam.x+'px,'+cam.y+'px) scale('+cam.k+')';
}
/* the root chip starts on the header's left line, like the old gutter did */
function initCam(){
  const c=canvasEl();
  cam={x:c?Math.max(26,(c.clientWidth-1180)/2+26):26,y:0,k:1};
}
function onPointerDown(e){
  const c=e.target.closest('.tv-canvas');
  if(!c||e.button!==0||!cam)return;
  panning=true;panMoved=false;
  downX=px=e.clientX;downY=py=e.clientY;
}
function onPointerMove(e){
  if(!panning)return;
  /* 4px of slack so a click on a chip is a click, not a 0px pan. Capture
     only once the pan is real: a captured pointer retargets the synthesized
     click at the canvas, which would eat every chip's expand click. */
  if(!panMoved){
    if(Math.hypot(e.clientX-downX,e.clientY-downY)<=4)return;
    panMoved=true;
    const c=canvasEl();
    if(c){c.classList.add('is-panning');c.setPointerCapture(e.pointerId);}
  }
  cam.x+=e.clientX-px;cam.y+=e.clientY-py;
  px=e.clientX;py=e.clientY;
  applyCam();
}
function onPointerUp(){
  if(!panning)return;
  panning=false;
  const c=canvasEl();
  if(c)c.classList.remove('is-panning');
}
function onWheel(e){
  const c=e.target.closest('.tv-canvas');
  if(!c||!cam)return;
  e.preventDefault();
  const nk=Math.max(K_MIN,Math.min(K_MAX,cam.k*Math.exp(-e.deltaY*.0016)));
  /* keep the layout point under the cursor under the cursor */
  const r=c.getBoundingClientRect();
  const mx=e.clientX-r.left,my=e.clientY-r.top;
  cam.x=mx-(mx-cam.x)/cam.k*nk;
  cam.y=my-(my-cam.y)/cam.k*nk;
  cam.k=nk;
  applyCam();
}

export default {
  /* No tab of its own: the Files tab owns the nav slot and this is its third
     lens, reached from the List | Graph | Tree pill (or #/tree). */
  id:'tree',label:'Tree',order:3,nav:false,parent:'projects',
  async mount(el,ctx){
    root=el;
    go=ctx.switchTo;
    root.innerHTML='<div class="tv"><div class="prj-note">loading the workspace…</div></div>';
    root.addEventListener('click',onClick);
    root.addEventListener('input',onInput);
    root.addEventListener('pointerdown',onPointerDown);
    root.addEventListener('pointermove',onPointerMove);
    root.addEventListener('pointerup',onPointerUp);
    root.addEventListener('pointercancel',onPointerUp);
    /* wheel must preventDefault (page zoom/scroll), so non-passive */
    root.addEventListener('wheel',onWheel,{passive:false});
    await load();
  },
  show(){if(model===null&&!loading)load();}
};

/* ── model ────────────────────────────────────────────────────────────────
   space.json is flat: hubs (projects), groups (a project's mapped folders,
   already split by depth with '__' separators) and leaves (files, carrying
   `path` = "<project>/<relative path>"). The real folder nesting is in the
   leaf paths, so the tree is rebuilt from those and the groups are ignored —
   using them would reproduce the graph's split-for-layout buckets, which are
   a rendering decision, not a directory structure. */
function build(data){
  const mk=(name,kind)=>({name,kind,dirs:new Map(),files:[],nFiles:0,nDirs:0});
  const workspace=mk(data.root?.label||'xo-projects','root');
  /* Seed from hubs, not from leaf paths: a project the scan found no mapped
     files in still exists, and silently dropping it would make the tree
     disagree with the List and the Graph about how many projects there are. */
  for(const hub of data.hubs||[]){
    const id=hub.id.replace(/^p_/,'');
    const node=mk(id,'project');
    node.label=hub.label||id;
    workspace.dirs.set(id,node);
  }

  for(const leaf of data.leaves||[]){
    const parts=String(leaf.path||'').split('/').filter(Boolean);
    if(parts.length<2)continue;
    let node=workspace;
    for(let i=0;i<parts.length-1;i++){
      const seg=parts[i];
      if(!node.dirs.has(seg))node.dirs.set(seg,mk(seg,i===0?'project':'dir'));
      node=node.dirs.get(seg);
    }
    /* project + project-relative path address the previewer's endpoint; the
       graph leaf id is kept for the "Graph" hand-off inside the previewer. */
    node.files.push({name:parts.at(-1),tag:leaf.tag||'',id:leaf.id,date:leaf.date||null,
      project:parts[0],path:parts.slice(1).join('/')});
  }
  /* Roll counts up so a collapsed branch can still say what it holds. */
  const roll=n=>{
    let f=n.files.length,d=n.dirs.size;
    for(const c of n.dirs.values()){const r=roll(c);f+=r.f;d+=r.d;}
    n.nFiles=f;n.nDirs=d;
    n.files.sort((a,b)=>a.name.localeCompare(b.name));
    return{f,d};
  };
  roll(workspace);
  return workspace;
}

async function load(){
  loading=true;
  const res=await apiFetch(API_BASE+'/xo/space.json');
  loading=false;
  if(!res.ok){
    root.querySelector('.tv').innerHTML=
      '<div class="prj-note">'+esc(res.offline?'xo-cowork-api is unreachable':res.error)+'</div>';
    return;
  }
  model=build(res.data);
  /* Start from the root: the workspace open, its projects closed. The first
     screen is an index of the workspace, not 1500 rows. */
  open=new Set(['']);
  render();
}

/* ── layout ───────────────────────────────────────────────────────────────
   One recursive pass. Each visible node reports the vertical band it needs;
   a parent centres itself on the span of its children, so the tree reads as
   a proper dendrogram rather than an indented list rotated 90°. */
function matches(n,q){
  if(!q)return true;
  if((n.label||n.name).toLowerCase().includes(q))return true;
  for(const f of n.files)if(f.name.toLowerCase().includes(q))return true;
  for(const c of n.dirs.values())if(matches(c,q))return true;
  return false;
}
/* 1 file ≈ hairline, 300 files ≈ 4px. log2 so a big project does not swamp
   everything else the way a linear scale would. */
const weight=n=>Math.max(1.1,Math.min(4.4,Math.log2(1+(n||0))*.62));
function layout(n,key,depth,top,q,out){
  const x=PAD_X+depth*COL_W;
  const isOpen=q?true:open.has(key);
  const kids=[...n.dirs.values()]
    .filter(c=>matches(c,q))
    .sort((a,b)=>(a.label||a.name).localeCompare(b.label||b.name));
  const files=q?n.files.filter(f=>f.name.toLowerCase().includes(q)):n.files;

  if(!isOpen||(!kids.length&&!files.length)){
    const node={key,x,y:top,n,depth,kind:n.kind,open:isOpen&&(kids.length||files.length)>0,
      hasKids:(n.dirs.size+n.files.length)>0};
    out.nodes.push(node);
    return{height:ROW_H,centre:top+ROW_H/2,node};
  }
  let cursor=top,first=null,last=null;
  const childCentres=[];
  for(const c of kids){
    const r=layout(c,key+'/'+c.name,depth+1,cursor,q,out);
    cursor+=r.height+GAP;
    childCentres.push(r.centre);
    if(first===null)first=r.centre;
    last=r.centre;
  }
  let stack=null;
  if(files.length){
    const all=expandedStacks.has(key);
    const shown=all?files:files.slice(0,FILES_SHOWN);
    const rows=shown.length+(files.length>shown.length?1:0);
    const h=rows*FILE_H+STACK_PAD*2+FILE_H; /* +1 row for the card's header */
    stack={x:x+COL_W,y:cursor,h,files:shown,extra:files.length-shown.length,
      total:files.length,centre:cursor+h/2,key};
    out.stacks.push(stack);
    childCentres.push(stack.centre);
    if(first===null)first=stack.centre;
    last=stack.centre;
    cursor+=h+GAP;
  }
  const height=Math.max(ROW_H,cursor-GAP-top);
  const centre=childCentres.length?(first+last)/2:top+ROW_H/2;
  const node={key,x,y:centre-ROW_H/2,n,depth,kind:n.kind,open:true,hasKids:true};
  out.nodes.push(node);
  /* One link per child, its weight set by how much that child holds: the
     trunk is thick, the twigs are thin. This is the whole "growth" read — a
     uniform 1px hairline makes a 300-file branch look like an empty one. */
  kids.forEach((c,i)=>out.links.push({
    x1:x+COL_W-24,y1:centre,x2:x+COL_W,y2:childCentres[i],w:weight(c.nFiles),
  }));
  if(stack)out.links.push({x1:x+COL_W-24,y1:centre,x2:x+COL_W,y2:stack.centre,
    w:weight(files.length),leaf:true});
  if(stack)stack.parent=node;
  return{height,centre,node};
}

/* ── render ─────────────────────────────────────────────────────────────── */
function render(){
  const q=filter.trim().toLowerCase();
  const out={nodes:[],links:[],stacks:[]};
  const r=layout(model,'',0,PAD_Y,q,out);
  const width=Math.max(...out.nodes.map(n=>n.x+COL_W),
    ...out.stacks.map(s=>s.x+COL_W-24),900)+PAD_X;
  const height=Math.max(r.height+PAD_Y*2,
    ...out.stacks.map(s=>s.y+s.h+PAD_Y),320);

  const grew=growing;growing=false;
  root.querySelector('.tv').innerHTML=
    head()
    +'<div class="tv-canvas"><div class="tv-surface'+(grew?' is-growing':'')
      +'" style="width:'+Math.round(width)
      +'px;height:'+Math.round(height)+'px">'
      +'<svg class="tv-links" width="'+Math.round(width)+'" height="'+Math.round(height)+'">'
        +out.links.map(l=>'<path pathLength="1" class="tv-link'+(l.leaf?' is-leaf':'')
          +'" style="stroke-width:'+l.w.toFixed(2)+'px" d="'+curve(l)+'"/>').join('')
      +'</svg>'
      +out.nodes.map(n=>nodeChip(n,!seen.has(n.key))).join('')
      +out.stacks.map(s=>stackBlock(s,!seen.has('stack:'+s.key))).join('')
    +'</div></div>';
  /* Anything that was not on screen last time animates in, so expanding a
     folder reads as that branch growing rather than as a repaint. */
  seen=new Set([...out.nodes.map(n=>n.key),...out.stacks.map(s=>'stack:'+s.key)]);
  /* drop the class once the draw-in has played, so a later re-render (filter,
     refresh) does not replay every branch */
  if(grew){
    const surface=root.querySelector('.tv-surface');
    setTimeout(()=>surface&&surface.classList.remove('is-growing'),480);
  }
  if(cam===null)initCam();
  applyCam();
  restoreAnchor();
  const input=root.querySelector('#tv-filter');
  if(input&&filter){input.focus();input.setSelectionRange(filter.length,filter.length);}
}
function head(){
  return'<div class="tv-head">'
    +'<span class="prj-eyebrow">'+plural(model.dirs.size,'project')+' · '
      +plural(model.nDirs,'folder')+' · '+plural(model.nFiles,'file')+'</span>'
    +'<span class="prj-spacer"></span>'
    +'<input class="tv-filter" id="tv-filter" placeholder="Filter by name…" '
      +'autocomplete="off" spellcheck="false" value="'+esc(filter)+'">'
    +'<button class="sess-refresh" data-tv="projects">Projects only</button>'
    +'<button class="sess-refresh" data-tv="reset">Reset view</button>'
    +'<button class="sess-refresh" data-tv="reload">&#8635; Refresh</button>'
  +'</div>';
}
/* An S-curve, not an elbow: at 228px of column width a bezier reads the
   parent→child direction at a glance without a corner every level. */
function curve(l){
  const mx=(l.x1+l.x2)/2;
  return`M${l.x1} ${l.y1} C${mx} ${l.y1} ${mx} ${l.y2} ${l.x2} ${l.y2}`;
}
function nodeChip(n,fresh){
  const label=n.n.label||n.n.name;
  const sub=n.n.nFiles+' file'+(n.n.nFiles===1?'':'s')
    +(n.n.nDirs?' · '+n.n.nDirs+' folder'+(n.n.nDirs===1?'':'s'):'');
  /* the bud grows with what the branch holds — the same signal as the link
     weight, readable when a chip sits alone on screen */
  const bud=Math.max(4,Math.min(11,3+Math.log2(1+n.n.nFiles)*1.5)).toFixed(1);
  return'<button class="tv-node is-'+n.kind+(n.open?' is-open':'')
      +(n.hasKids?'':' is-empty')+(fresh?' is-new':'')
      +'" style="left:'+Math.round(n.x)+'px;top:'+Math.round(n.y)+'px'
      +';--bud:'+bud+'px;--depth:'+Math.min(n.depth,5)+'" '
      +'data-key="'+esc(n.key)+'" title="'+esc(label)+'" '
      +'aria-expanded="'+(n.hasKids?(n.open?'true':'false'):'false')+'">'
    +'<span class="tv-bud" aria-hidden="true"></span>'
    +'<span class="tv-label"><span class="tv-name">'+esc(label)+'</span>'
      +'<span class="tv-count">'+esc(sub)+'</span></span>'
    +(n.hasKids?'<span class="tv-caret" aria-hidden="true">'
      +(n.open?'&#9662;':'&#9656;')+'</span>':'')
  +'</button>';
}
/* Leaves expand vertically: one card per folder, not one column per file.
   The card is what makes them read as fruit on that branch instead of loose
   text floating in the gutter. */
function stackBlock(s,fresh){
  return'<div class="tv-stack'+(fresh?' is-new':'')+'" style="left:'+Math.round(s.x)
      +'px;top:'+Math.round(s.y)+'px;min-height:'+Math.round(s.h)+'px">'
    +'<div class="tv-stack-head">'+s.total+' file'+(s.total===1?'':'s')+'</div>'
    +s.files.map(f=>'<button class="tv-leaf" data-file="'+esc(f.path||'')+'" '
      +'data-project="'+esc(f.project||'')+'" title="'+esc(f.name)+'">'
      +'<span class="tv-name">'+esc(f.name)+'</span>'
      +'<span class="tv-tag">'+esc(f.tag||'')+'</span>'
    +'</button>').join('')
    +(s.extra
      ?'<button class="tv-leaf is-more" data-stack="'+esc(s.key)+'">+'+s.extra
        +' more</button>'
      :(expandedStacks.has(s.key)&&s.total>FILES_SHOWN
        ?'<button class="tv-leaf is-more" data-stack="'+esc(s.key)+'">show fewer</button>'
        :''))
  +'</div>';
}

/* ── interaction ──────────────────────────────────────────────────────────
   Clicking a file opens the previewer; it does not move you. The Graph
   hand-off lives inside the previewer, so browsing the tree never costs you
   your place in it. */
function onClick(e){
  /* a drag that ended on a chip is a pan, not a click */
  if(panMoved){panMoved=false;return;}
  const act=e.target.closest('[data-tv]');
  if(act){
    if(act.dataset.tv==='projects'){open=new Set(['']);expandedStacks.clear();render();}
    else if(act.dataset.tv==='reset'){initCam();applyCam();}
    else{model=null;root.querySelector('.tv').innerHTML='<div class="prj-note">loading…</div>';load();}
    return;
  }
  const node=e.target.closest('[data-key]');
  if(node){
    const k=node.dataset.key;
    keepAnchor(k);
    if(open.has(k)){open.delete(k);}
    else{open.add(k);growing=true;}
    render();
    return;
  }
  const more=e.target.closest('[data-stack]');
  if(more){
    const k=more.dataset.stack;
    keepAnchor(k);growing=true;
    if(expandedStacks.has(k))expandedStacks.delete(k);else expandedStacks.add(k);
    render();
    return;
  }
  const file=e.target.closest('[data-file]');
  if(file&&file.dataset.file){
    /* Preview in the side drawer; the tree keeps its scroll, its expansion
       state and its place. Jumping to the Graph is the Graph button inside
       the previewer — an explicit choice, not the cost of opening a file. */
    dispatchEvent(new CustomEvent('space:preview-file',{detail:{
      project:file.dataset.project,path:file.dataset.file,
      name:file.querySelector('.tv-name').textContent}}));
  }
}
/* The surface is rebuilt on every render AND the layout reflows around the
   node you just opened — a parent re-centres over its taller subtree, which
   can shove the whole tree (root included) off screen. The camera alone is
   not enough; the fix is to pin the clicked node to the screen position it
   already had and let the tree grow around it. Offsets are screen px, so
   the same arithmetic holds at any zoom. */
function keepAnchor(key){
  anchor=null;
  if(!key)return;
  const c=canvasEl(),el=root.querySelector('[data-key="'+CSS.escape(key)+'"]');
  if(!c||!el)return;
  const cr=c.getBoundingClientRect(),er=el.getBoundingClientRect();
  anchor={key,left:er.left-cr.left,top:er.top-cr.top};
}
function restoreAnchor(){
  if(!anchor)return;
  const c=canvasEl(),el=root.querySelector('[data-key="'+CSS.escape(anchor.key)+'"]');
  if(c&&el){
    const cr=c.getBoundingClientRect(),er=el.getBoundingClientRect();
    cam.x+=anchor.left-(er.left-cr.left);
    cam.y+=anchor.top-(er.top-cr.top);
    applyCam();
  }
  anchor=null;
}
let debounce=null;
function onInput(e){
  if(e.target.id!=='tv-filter')return;
  filter=e.target.value;
  clearTimeout(debounce);
  debounce=setTimeout(render,140);
}
