/* The atlas views (Dashboard, Graph, Timeline): lenses over one selected
   graph dataset. Dashboard uses dashboard.json while Graph uses space.json.
   They share the model, camera, selection state and cross-view actions
   inside one boot() closure, so they live in one module exporting three
   views (splitting them would force cross-imports, which the view contract
   forbids). Cross-view jumps go through ctx.switchTo (`go`). All graph
   content comes from the workspace's .xo/space.json, served at /xo/space.json;
   nothing is embedded here. */
import {API_BASE,apiFetch} from '../core/api.js';
import {toast} from '../core/ui.js';

let go=()=>{};   /* ctx.switchTo, captured on first mount */
const hooks={};  /* boot() assigns lifecycle hooks here once it has run */
let bootPromise=null;
let bootDataset=null;

/* Cross-lens focus: the List's "Map" action and the previewer's Graph button
   dispatch space:focus-project; if the graph has not booted yet the request is
   parked until boot consumes it. (The lens switch itself is shell chrome —
   core/lens-switch.js — so it cannot move when the lens changes.) */
let pendingFocus=null;
addEventListener('space:focus-project',e=>{
  pendingFocus=String(e.detail||'');
  if(hooks.focusProject)hooks.focusProject();
});

const DATASETS={
  dashboard:{url:API_BASE+'/xo/dashboard.json',label:'Dashboard'},
  graph:{url:API_BASE+'/xo/space.json',label:'Graph'}
};
const DATASET_KEY='space.atlasDataset';

function savedDataset(){
  try{
    const value=localStorage.getItem(DATASET_KEY);
    return DATASETS[value]?value:'graph';
  }catch(_err){return'graph';}
}

function rememberDataset(dataset){
  try{localStorage.setItem(DATASET_KEY,dataset);}catch(_err){}
}

/* boot() runs exactly once, no matter which atlas lens mounts first or how
   many mount concurrently. Switching between the two graph projections
   reloads once, matching main's dataset switch and resetting the simulation. */
function ensureBoot(requestedDataset){
  const dataset=DATASETS[requestedDataset]?requestedDataset:savedDataset();
  if(bootPromise&&bootDataset!==dataset){
    rememberDataset(dataset);
    location.reload();
    return new Promise(()=>{});
  }
  bootDataset=dataset;
  rememberDataset(dataset);
  if(!bootPromise)bootPromise=(async()=>{
    const source=DATASETS[dataset];
    const res=await apiFetch(source.url);
    if(!res.ok){
      console.warn('Space could not load '+source.url+':',res.error);
      throw new Error(res.error);
    }
    boot(res.data,source.label);
  })();
  return bootPromise;
}

function renderNoData(el,dataset){
  if(!el)return;
  const source=DATASETS[dataset]||DATASETS[savedDataset()];
  const box=document.createElement('div');
  box.className='nodata';
  box.innerHTML='<div class="eyebrow">No data source</div>'+
    '<h1>Space reads its map from a local file.</h1>'+
    '<p>This page loads <b>'+source.url+'</b> — a file in the workspace <b>.xo</b> directory — from this local server, so the data stays on this machine. Start the workspace server:</p>'+
    '<pre>cd xo-space && ./cowork-api.sh start</pre>'+
    '<p>then open <b>http://localhost:5002/space/</b></p>'+
    '<button id="nodata-retry">Retry</button>';
  el.appendChild(box);
  box.querySelector('#nodata-retry').addEventListener('click',()=>location.reload());
}

function atlasView(id,label,order,lens,dataset=null){
  return{
    id,label,order,
    async mount(el,ctx){
      go=ctx.switchTo;
      el.querySelectorAll('[data-atlas-lens]').forEach(button=>{
        button.addEventListener('click',()=>go(button.dataset.atlasLens));
      });
      try{await ensureBoot(dataset);}
      catch(err){renderNoData(el,dataset);}
    },
    show(){if(hooks.setActiveView)hooks.setActiveView(lens);},
    hide(){if(hooks.setActiveView)hooks.setActiveView(null);}
  };
}
export const dashboardView={
  ...atlasView('dashboard','Dashboard',0,'graph','dashboard'),
  section:'graph'
};
/* Files lands on the List lens (the projects view owns the nav tab); the
   Graph is its second lens, reachable from the pill or #/graph. */
export const graphView={
  ...atlasView('graph','Graph',1,'graph','graph'),
  nav:false,parent:'projects'
};
/* Timeline is pinned to the workspace dataset (space.json): plotting the
   Dashboard's 5-environment projection there has no git history and reads
   as broken. Arriving from Dashboard costs one dataset-switch reload, the
   same hop Dashboard ↔ Files already makes. */
export const timeView=atlasView('time','Timeline',2,'time','graph');

function boot(DATA,DATA_SOURCE){
/* ============================== MODEL FROM LOCAL DATA ==============================
   All graph content comes from .xo/space.json (GET /xo/space.json); nothing is
   embedded here. */
const CAT=DATA.categories;
const ACCENT='#a8d94f', ACCENT_DEEP='#83d63a';
const graphRoute=bootDataset==='dashboard'?'dashboard':'graph';
const hubLabel=DATA.meta.hubLabel||'Department';
const NODES=[];
NODES.push({id:DATA.root.id,type:'root',label:DATA.root.label,blurb:DATA.root.blurb});
DATA.hubs.forEach(h=>NODES.push({id:h.id,type:'hub',cat:h.cat,label:h.label,blurb:h.blurb}));
DATA.groups.forEach(g=>NODES.push({id:g.id,type:'group',cat:g.cat,label:g.label,blurb:g.blurb}));
DATA.leaves.forEach(l=>NODES.push({
  id:l.id,type:'leaf',group:l.group,shape:l.shape,tag:l.tag,label:l.label,
  date:l.date,blurb:l.blurb,path:l.path,clusters:l.clusters||[],xotype:l.xotype
}));
const EDGES=[];
DATA.hubs.forEach(h=>EDGES.push({s:DATA.root.id,t:h.id,kind:'root',label:DATA.meta.rootEdgeLabel||'a department of XO'}));
DATA.groups.forEach(g=>EDGES.push({s:g.cat,t:g.id,kind:'hg',label:'part of'}));
DATA.leaves.forEach(l=>EDGES.push({s:l.group,t:l.id,kind:'rg',label:'part of'}));
DATA.ties.forEach(x=>EDGES.push({s:x.s,t:x.t,kind:'x',label:x.label}));

/* ============================== MODEL ============================== */
const byId=new Map(NODES.map(n=>[n.id,n]));
NODES.forEach(n=>{
  if(n.type==='leaf') n.cat=byId.get(n.group).cat;
  n.adj=[];n.x=0;n.y=0;n.vx=0;n.vy=0;n.fx=null;n.fy=null;
});
EDGES.forEach(e=>{byId.get(e.s).adj.push({e,other:e.t});byId.get(e.t).adj.push({e,other:e.s});});
NODES.forEach(n=>n.degree=n.adj.length);
const LEAVES=NODES.filter(n=>n.type==='leaf');
const GROUPS=NODES.filter(n=>n.type==='group');
const HUBS=NODES.filter(n=>n.type==='hub');
const XCOUNT=EDGES.filter(e=>e.kind==='x').length;
const noun=DATA.meta.noun||'artifacts';
const collectionLabel=DATA.meta.collectionLabel||'clusters';
document.getElementById('q').placeholder=`Search ${LEAVES.length} ${noun}…`;
document.getElementById('fmeta').textContent=
  `${LEAVES.length} ${noun} · ${GROUPS.length} ${collectionLabel} · ${EDGES.length} links · mapped ${DATA.meta.mappedOn} · data: ${DATA_SOURCE}`;
if(DATA.meta.timelineTitle){
  document.querySelector('#view-time .thead h2').textContent=DATA.meta.timelineTitle;
}
if(DATA.meta.timelineSub){
  document.getElementById('tsub').textContent=DATA.meta.timelineSub;
}

const colorOf=n=>n.type==='root'?'#e9e4d9':CAT[n.cat].color;
function radiusOf(n){
  if(n.type==='root')return 17;
  if(n.type==='hub')return 13;
  if(n.type==='group')return 5.5+Math.min(5,n.adj.length*.22);
  return 3.3+Math.min(4.2,(n.degree-1)*.85);
}
NODES.forEach(n=>n.r=radiusOf(n));
const fmtDate=d=>new Date(d+'T00:00:00').toLocaleDateString('en-US',{year:'numeric',month:'long',day:'numeric'});
const fmtMY=t=>new Date(t).toLocaleDateString('en-US',{year:'numeric',month:'short'});
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const hexA=(h,a)=>`rgba(${parseInt(h.slice(1,3),16)},${parseInt(h.slice(3,5),16)},${parseInt(h.slice(5,7),16)},${a})`;
const REDUCED=matchMedia('(prefers-reduced-motion: reduce)').matches;

/* expansion + filter state */
const expanded=new Map(GROUPS.map(g=>[g.id,true]));
let deptFilter=null;
const belongsToCategory=(n,cat)=>n.cat===cat||(n.clusters||[]).includes(cat);
const isShown=n=>{
  if(n.type==='leaf'){
    if(!expanded.get(n.group))return false;
    if(deptFilter&&!belongsToCategory(n,deptFilter))return false;
    return true;
  }
  return true;
};
const dimByFilter=n=>deptFilter&&n.cat&&!belongsToCategory(n,deptFilter);
const shownNodes=()=>NODES.filter(isShown);
const shownEdges=()=>EDGES.filter(e=>isShown(byId.get(e.s))&&isShown(byId.get(e.t)));

/* layout seed */
const HUB_ANGLE=DATA.hubAngles;
const HUB_R=520;
/* root id comes from the data — never hardcode it ('xo' today, anything
   tomorrow); byId.get(unknown).fx throws and kills boot. */
const root=byId.get(DATA.root.id);root.fx=0;root.fy=0;
document.getElementById('root-name').textContent=DATA.root.label;
document.getElementById('root-reset').textContent='Reset to '+DATA.root.label;
HUBS.forEach(h=>{h.ax=Math.cos(HUB_ANGLE[h.cat])*HUB_R;h.ay=Math.sin(HUB_ANGLE[h.cat])*HUB_R;h.x=h.ax;h.y=h.ay;});
/* Each project owns an equal sector of the circle; its cluster fan must stay
   inside it. A fixed .5 rad step wraps the whole circle once a project has
   ~13+ clusters (generated data easily does), seeding clusters in other
   projects' territory — and the hub spring constrains distance, not angle,
   so they never migrate home. Radius staggers to relieve arc crowding. */
const SECTOR=Math.PI*2/Math.max(1,Object.keys(HUB_ANGLE).length);
GROUPS.forEach(g=>{
  const sib=GROUPS.filter(x=>x.cat===g.cat),k=sib.indexOf(g),m=sib.length;
  const step=Math.min(.5,SECTOR*.85/Math.max(1,m));
  const a=HUB_ANGLE[g.cat]+(k-(m-1)/2)*step;
  const r=HUB_R+170+(k%3)*70;
  g.x=Math.cos(a)*r;g.y=Math.sin(a)*r;
});
LEAVES.forEach((l,i)=>{
  const g=byId.get(l.group);
  const a=(i*.618033*Math.PI*2)%(Math.PI*2);
  l.x=g.x+Math.cos(a)*(30+ (i%5)*11);
  l.y=g.y+Math.sin(a)*(30+ (i%5)*11);
});

/* ============================== SIMULATION ============================== */
let simAlpha=1;
let rootId=DATA.root.id,rootDepths=null;
const SPR={
  root:{d:HUB_R,k:.02},
  hg:{d:175,k:.05},
  rg:{d:62,k:.08},
  x:DATA.meta.tieSpring||{d:210,k:.005}
};
const CHG={root:-3400,hub:-2600,group:-1000,leaf:-235};
function simTick(){
  const vs=shownNodes(),es=shownEdges();
  for(let i=0;i<vs.length;i++){
    const a=vs[i],qa=CHG[a.type]||CHG.leaf;
    for(let j=i+1;j<vs.length;j++){
      const b=vs[j];
      let dx=b.x-a.x,dy=b.y-a.y,d2=dx*dx+dy*dy;
      if(d2<1){dx=Math.random()-.5;dy=Math.random()-.5;d2=1;}
      if(d2>320*320)continue;
      const d=Math.sqrt(d2),qb=CHG[b.type]||CHG.leaf;
      let f=Math.min(qa,qb)/d2*simAlpha;
      const rr=a.r+b.r+7;
      if(d<rr)f-=(rr-d)*.3;
      const fx=dx/d*f,fy=dy/d*f;
      if(a.fx==null){a.vx+=fx;a.vy+=fy;}
      if(b.fx==null){b.vx-=fx;b.vy-=fy;}
    }
  }
  for(const e of es){
    const a=byId.get(e.s),b=byId.get(e.t),sp=SPR[e.kind];
    let dx=b.x-a.x,dy=b.y-a.y;
    const d=Math.max(1,Math.hypot(dx,dy)),f=(d-sp.d)*sp.k*simAlpha;
    const fx=dx/d*f,fy=dy/d*f;
    if(a.fx==null){a.vx+=fx;a.vy+=fy;}
    if(b.fx==null){b.vx-=fx;b.vy-=fy;}
  }
  const R0=byId.get(rootId);
  for(const n of vs){
    if(rootId===DATA.root.id&&n.type==='hub'){n.vx+=(n.ax-n.x)*.05*simAlpha;n.vy+=(n.ay-n.y)*.05*simAlpha;}
    else if(rootDepths&&n.id!==rootId&&n.fx==null){
      /* concentric neighbourhood rings around the chosen root */
      const d=rootDepths.get(n.id)??6;
      let dx=n.x-R0.x,dy=n.y-R0.y;
      let dist=Math.hypot(dx,dy);
      if(dist<1){dx=Math.random()-.5;dy=Math.random()-.5;dist=1;}
      const f=(d*110-dist)*.045*simAlpha;
      n.vx+=dx/dist*f;n.vy+=dy/dist*f;
    }
    else if(n.fx==null){n.vx-=(n.x-R0.x)*.001*simAlpha;n.vy-=(n.y-R0.y)*.001*simAlpha;}
    if(n.fx!=null){n.x=n.fx;n.y=n.fy;n.vx=0;n.vy=0;continue;}
    n.vx*=.7;n.vy*=.7;
    /* Speed limit: with generated data a group can own 100+ leaves, whose
       summed spring stiffness makes explicit Euler diverge (positions hit
       1e20 and the camera fit goes with them). Clamping per-tick velocity
       bounds the integrator regardless of cluster size. */
    const _sp=Math.hypot(n.vx,n.vy);
    if(_sp>60){n.vx*=60/_sp;n.vy*=60/_sp;}
    n.x+=n.vx;n.y+=n.vy;
  }
  /* Decay to a full stop instead of idling at .02 forever — a perpetual 2%
     simmer keeps every force (incl. the centering bias) acting for eternity,
     so the layout jiggles when zoomed and whole projects drift toward the
     root. Interactions reheat() as before. */
  if(simAlpha>.003)simAlpha*=.9885;else simAlpha=0;
}
const reheat=a=>{simAlpha=Math.max(simAlpha,a);};

/* ============================== CAMERA ============================== */
const cam={x:0,y:0,k:.7};
let camAnim=null;
const easeCubicInOut=t=>t<.5?4*t*t*t:1-Math.pow(-2*t+2,3)/2;
function flyTo(x,y,k,ms=820){
  if(REDUCED)ms=1;
  camAnim={t0:performance.now(),ms,from:{...cam},to:{x,y,k}};
}
function stepCam(now){
  if(!camAnim)return;
  const t=Math.min(1,(now-camAnim.t0)/camAnim.ms),e=easeCubicInOut(t);
  cam.x=camAnim.from.x+(camAnim.to.x-camAnim.from.x)*e;
  cam.y=camAnim.from.y+(camAnim.to.y-camAnim.from.y)*e;
  cam.k=camAnim.from.k+(camAnim.to.k-camAnim.from.k)*e;
  if(t>=1)camAnim=null;
}
function fitNodes(ids,pad=120,kmax=2.2){
  const ns=ids.map(id=>byId.get(id));
  let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
  ns.forEach(n=>{x0=Math.min(x0,n.x);y0=Math.min(y0,n.y);x1=Math.max(x1,n.x);y1=Math.max(y1,n.y);});
  const k=Math.max(.25,Math.min(kmax,.9*Math.min(GW/(x1-x0+pad),GH/(y1-y0+pad))));
  flyTo((x0+x1)/2,(y0+y1)/2,k,900);
}

/* ============================== GRAPH RENDER ============================== */
const gcv=document.getElementById('gcanvas'),gc=gcv.getContext('2d');
let GW=0,GH=0,dpr=1;
let hoverId=null,selId=null,focusSet=null,focusDepth=0;
let pathIds=null,pathEdges=null,pathReveal=0;
function neighborhood(id,depth){
  const set=new Set([id]);
  let frontier=[id];
  for(let d=0;d<depth;d++){
    const next=[];
    for(const u of frontier)for(const {other} of byId.get(u).adj){
      if(!set.has(other)&&isShown(byId.get(other))){set.add(other);next.push(other);}
    }
    frontier=next;
  }
  return set;
}
function drawShape(c,x,y,r,shape){
  c.beginPath();
  if(shape==='diamond'){const s=r*1.25;c.moveTo(x,y-s);c.lineTo(x+s,y);c.lineTo(x,y+s);c.lineTo(x-s,y);c.closePath();}
  else if(shape==='slab'){const w=r*1.55,h=r*.95;c.rect(x-w,y-h,w*2,h*2);}
  else if(shape==='stack'){const s=r*.92,o=r*.38;c.rect(x-s-o,y-s+o,s*2,s*2);c.rect(x-s+o,y-s-o,s*2,s*2);}
  else c.arc(x,y,r,0,Math.PI*2);
}
function convexHull(points){
  const pts=[...points].sort((a,b)=>a[0]-b[0]||a[1]-b[1]);
  if(pts.length<=1)return pts;
  const cross=(o,a,b)=>(a[0]-o[0])*(b[1]-o[1])-(a[1]-o[1])*(b[0]-o[0]);
  const lower=[];
  for(const point of pts){
    while(lower.length>=2&&cross(lower.at(-2),lower.at(-1),point)<=0)lower.pop();
    lower.push(point);
  }
  const upper=[];
  for(let i=pts.length-1;i>=0;i--){
    const point=pts[i];
    while(upper.length>=2&&cross(upper.at(-2),upper.at(-1),point)<=0)upper.pop();
    upper.push(point);
  }
  lower.pop();upper.pop();
  return lower.concat(upper);
}
function drawEnclosures(k){
  const PAD=42;
  for(const group of GROUPS){
    if(deptFilter&&group.cat!==deptFilter)continue;
    const points=[[group.x,group.y]];
    for(const leaf of LEAVES){
      if(isShown(leaf)&&belongsToCategory(leaf,group.cat)){
        points.push([leaf.x,leaf.y]);
      }
    }
    if(points.length<2)continue;
    const col=CAT[group.cat]?.color||'#888888';
    const cx=points.reduce((sum,point)=>sum+point[0],0)/points.length;
    const cy=points.reduce((sum,point)=>sum+point[1],0)/points.length;
    gc.beginPath();
    if(points.length===2){
      const radius=Math.hypot(points[1][0]-points[0][0],points[1][1]-points[0][1])/2+PAD;
      gc.arc(cx,cy,radius,0,Math.PI*2);
    }else{
      const hull=convexHull(points).map(point=>{
        const dx=point[0]-cx,dy=point[1]-cy;
        const distance=Math.hypot(dx,dy)||1;
        return [point[0]+dx/distance*PAD,point[1]+dy/distance*PAD];
      });
      const first=hull[0],last=hull.at(-1);
      gc.moveTo((last[0]+first[0])/2,(last[1]+first[1])/2);
      for(let i=0;i<hull.length;i++){
        const point=hull[i],next=hull[(i+1)%hull.length];
        gc.quadraticCurveTo(
          point[0],point[1],
          (point[0]+next[0])/2,(point[1]+next[1])/2
        );
      }
      gc.closePath();
    }
    gc.fillStyle=hexA(col,.055);gc.fill();
    gc.setLineDash([5/k,4/k]);
    gc.strokeStyle=hexA(col,.32);
    gc.lineWidth=1.2/Math.sqrt(k);
    gc.stroke();
    gc.setLineDash([]);
  }
}
function drawGraph(now){
  gc.setTransform(dpr,0,0,dpr,0,0);
  gc.clearRect(0,0,GW,GH);
  /* ambient tints */
  let grd=gc.createRadialGradient(GW*.74,GH*.32,0,GW*.74,GH*.32,GW*.5);
  grd.addColorStop(0,'rgba(168,217,79,.05)');grd.addColorStop(1,'rgba(0,0,0,0)');
  gc.fillStyle=grd;gc.fillRect(0,0,GW,GH);
  grd=gc.createRadialGradient(GW*.2,GH*.8,0,GW*.2,GH*.8,GW*.45);
  grd.addColorStop(0,'rgba(111,147,173,.04)');grd.addColorStop(1,'rgba(0,0,0,0)');
  gc.fillStyle=grd;gc.fillRect(0,0,GW,GH);

  stepCam(now);
  const k=cam.k;
  gc.setTransform(dpr*k,0,0,dpr*k,dpr*(GW/2-cam.x*k),dpr*(GH/2-cam.y*k));
  const es=shownEdges(),vs=shownNodes();
  layoutSats(now);
  const inFocus=id=>!focusSet||focusSet.has(id);
  if(DATA.meta.enclose)drawEnclosures(k);
  /* path reveal progress */
  let revealSeg=1e9;
  if(pathIds){
    const per=REDUCED?0:420;
    revealSeg=per?Math.min(pathIds.length,(now-pathReveal)/per):1e9;
  }
  /* ---- edges ---- */
  for(const e of es){
    if(DATA.meta.enclose&&deptFilter&&e.kind==='root')continue;
    const a=byId.get(e.s),b=byId.get(e.t);
    let alpha,width,color;
    if(pathIds){
      const idx=pathEdges?pathEdges.indexOf(e):-1;
      if(idx>=0&&idx<revealSeg){alpha=.85;width=2/k;color=ACCENT;}
      else{alpha=.015;width=.7/k;color='#cfc9bb';}
    }else if(focusSet){
      const lit=(e.s===selId||e.t===selId)&&inFocus(e.s)&&inFocus(e.t);
      const semi=inFocus(e.s)&&inFocus(e.t);
      if(lit){alpha=.42;width=1.4/k;color=ACCENT;}
      else if(semi){alpha=.14;width=.8/k;color='#cfc9bb';}
      else{alpha=.012;width=.7/k;color='#78746c';}
    }else{
      const fdim=dimByFilter(a)||dimByFilter(b);
      alpha=(e.kind==='x'?.10:e.kind==='root'?.07:.05)*(fdim?.25:1);
      width=(e.kind==='x'?.9:.7)/k;color=e.kind==='x'?'#cfc9bb':'#b4afa4';
    }
    gc.beginPath();
    if(e.kind==='x'){
      const dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy)||1;
      gc.moveTo(a.x,a.y);
      gc.quadraticCurveTo((a.x+b.x)/2-dy/d*d*.13,(a.y+b.y)/2+dx/d*d*.13,b.x,b.y);
    }else{gc.moveTo(a.x,a.y);gc.lineTo(b.x,b.y);}
    gc.strokeStyle=hexA(color,alpha);gc.lineWidth=width;gc.lineCap='round';gc.stroke();
  }
  drawSatOrbits(k);
  /* ---- nodes ---- */
  const drawOrder=pathIds?[...vs].sort((a,b)=>(pathIds.includes(a.id)?1:0)-(pathIds.includes(b.id)?1:0)):vs;
  for(const n of drawOrder){
    if(DATA.meta.enclose&&deptFilter&&n.type==='root')continue;
    const col=colorOf(n);
    let a=1;
    if(pathIds)a=pathIds.includes(n.id)?1:.10;
    else if(focusSet)a=focusSet.has(n.id)?1:.14;
    else if(dimByFilter(n))a=.18;
    gc.globalAlpha=a;
    if(n.type==='root'){
      /* the actual XO mark: white X chevrons, lime O chevrons */
      gc.lineWidth=2.4/Math.sqrt(k);gc.lineJoin='miter';gc.lineCap='butt';
      const sc=.075;
      const CHEV=[
        ['#e9e4d9',[[37,166],[118,247],[31,335]]],
        ['#e9e4d9',[[245,166],[163,247],[251,335]]],
        [ACCENT_DEEP,[[328,165],[247,247],[334,334]]],
        [ACCENT_DEEP,[[381,165],[462,247],[375,334]]],
      ];
      for(const[col,pts]of CHEV){
        gc.strokeStyle=col;gc.beginPath();
        pts.forEach(([px,py],i)=>{
          const wx=n.x+(px-246.5)*sc,wy=n.y+(py-250)*sc;
          i?gc.lineTo(wx,wy):gc.moveTo(wx,wy);
        });
        gc.stroke();
      }
    }else if(n.type==='hub'){
      gc.beginPath();gc.arc(n.x,n.y,n.r,0,Math.PI*2);
      gc.fillStyle=hexA(col,.13);gc.fill();
      gc.strokeStyle=hexA(col,.9);gc.lineWidth=1.4/Math.sqrt(k);gc.stroke();
      gc.beginPath();gc.arc(n.x,n.y,2.6,0,Math.PI*2);gc.fillStyle=col;gc.fill();
    }else if(n.type==='group'){
      gc.beginPath();gc.arc(n.x,n.y,n.r,0,Math.PI*2);
      gc.fillStyle=hexA(col,.22);gc.fill();
      gc.strokeStyle=hexA(col,.8);gc.lineWidth=1.1/Math.sqrt(k);gc.stroke();
      if(!expanded.get(n.id)){
        gc.beginPath();gc.arc(n.x,n.y,n.r+3.2,0,Math.PI*2);
        gc.setLineDash([2.4/k,3.2/k]);
        gc.strokeStyle=hexA(col,.4);gc.lineWidth=.9/Math.sqrt(k);gc.stroke();
        gc.setLineDash([]);
      }
    }else{
      const hl=n.id===hoverId||n.id===selId||(pathIds&&pathIds.includes(n.id));
      const r=n.r*(hl?1.5:1);
      drawShape(gc,n.x,n.y,r,n.shape);
      if(n.shape==='ring'){
        gc.strokeStyle=col;gc.lineWidth=1.5/Math.sqrt(k);gc.stroke();
      }else{gc.fillStyle=col;gc.fill();}
      if(n.id===selId||(pathIds&&pathIds.includes(n.id))){
        drawShape(gc,n.x,n.y,r+3.4/Math.sqrt(k),n.shape);
        gc.strokeStyle=hexA(ACCENT,.8);gc.lineWidth=1.4/Math.sqrt(k);gc.stroke();
      }else if(hl){
        drawShape(gc,n.x,n.y,r+3/Math.sqrt(k),n.shape);
        gc.strokeStyle='rgba(233,228,217,.9)';gc.lineWidth=1.2/Math.sqrt(k);gc.stroke();
      }
    }
    if(n.id===rootId&&n.type!=='root'){
      gc.beginPath();gc.arc(n.x,n.y,n.r+7/Math.sqrt(k),0,Math.PI*2);
      gc.strokeStyle=hexA(ACCENT,.65);gc.lineWidth=1.4/Math.sqrt(k);gc.stroke();
      gc.beginPath();gc.arc(n.x,n.y,n.r+11/Math.sqrt(k),0,Math.PI*2);
      gc.strokeStyle=hexA(ACCENT,.2);gc.lineWidth=1/Math.sqrt(k);gc.stroke();
    }
    gc.globalAlpha=1;
  }
  drawSatDots(now,k);
  /* ---- labels (screen space) ---- */
  gc.setTransform(dpr,0,0,dpr,0,0);
  gc.textAlign='center';
  for(const n of vs){
    let a=1;
    if(pathIds)a=pathIds.includes(n.id)?1:0;
    else if(focusSet)a=focusSet.has(n.id)?1:0;
    else if(dimByFilter(n))a=.25;
    if(a===0)continue;
    const sx=(n.x-cam.x)*k+GW/2,sy=(n.y-cam.y)*k+GH/2;
    if(sx<-100||sx>GW+100||sy<-50||sy>GH+50)continue;
    if(n.type==='hub'){
      gc.font='500 17px '+SERIF;
      halo(n.label,sx,sy-n.r*k-12,`rgba(233,228,217,${.94*a})`);
      gc.font='400 8.5px '+MONO;
      const hubCount=LEAVES.filter(l=>belongsToCategory(l,n.cat)).length;
      const hubNoun=hubCount===1?noun.replace(/s$/,''):noun;
      halo(`${hubCount} ${hubNoun.toUpperCase()}`,sx,sy+n.r*k+16,`rgba(125,120,109,${a})`,.14);
    }else if(n.type==='group'){
      const on=n.id===hoverId||n.id===selId||(focusSet&&focusSet.has(n.id));
      if(!(on||k>.8))continue;
      const closed=!expanded.get(n.id);
      gc.font='400 9px '+MONO;
      const t=n.label.toUpperCase()+(closed?` +${LEAVES.filter(l=>belongsToCategory(l,n.cat)).length}`:'');
      halo(t,sx,sy-n.r*k-7,`rgba(179,173,160,${.72*a})`,.1);
    }else if(n.type==='leaf'){
      const on=n.id===hoverId||n.id===selId||n.id===rootId||(focusSet&&focusSet.has(n.id))||(pathIds&&pathIds.includes(n.id));
      if(!(on||k>1.55||(k>1.05&&n.degree>=4)))continue;
      gc.font='400 11px '+SANS;
      halo(n.label,sx,sy-n.r*k-7,on?`rgba(233,228,217,${.94*a})`:`rgba(179,173,160,${.62*a})`);
    }
  }
  drawSatLabels(k);
  gc.globalAlpha=1;
  /* pulse ring */
  if(pulseN){
    const t=(now-pulseN.t0)/1100;
    if(t>1)pulseN=null;
    else{
      const n=byId.get(pulseN.id);
      const sx=(n.x-cam.x)*k+GW/2,sy=(n.y-cam.y)*k+GH/2;
      gc.beginPath();gc.arc(sx,sy,n.r*k+t*44,0,Math.PI*2);
      gc.strokeStyle=hexA(ACCENT,.7*(1-t));gc.lineWidth=1.8;gc.stroke();
    }
  }
  /* settling status */
  document.getElementById('simstat').style.opacity=simAlpha>.05?1:0;
}
const SERIF=`"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif`;
const SANS=`system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif`;
const MONO=`ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace`;
function halo(s,x,y,fill,tracking){
  if(tracking){gc.save();/* cheap letterspacing for tiny mono caps */
    gc.letterSpacing=(tracking*10)+'px';}
  gc.lineWidth=3.5;gc.strokeStyle='rgba(11,12,15,.88)';gc.lineJoin='round';
  gc.strokeText(s,x,y);gc.fillStyle=fill;gc.fillText(s,x,y);
  if(tracking)gc.restore();
}
let pulseN=null;

/* ============================== GRAPH INTERACTION ============================== */
let drag=null,pan=false,downX=0,downY=0,moved=false,lastX=0,lastY=0;
const toWorld=(mx,my)=>({x:(mx-GW/2)/cam.k+cam.x,y:(my-GH/2)/cam.k+cam.y});
/* Pointer events give viewport coordinates, but drawing/toWorld are in
   canvas-local space and the canvas sits below the header — feed clientX/Y
   in directly and every hit test lands one header-height off. */
const evXY=e=>{const r=gcv.getBoundingClientRect();return[e.clientX-r.left,e.clientY-r.top];};
function pick(mx,my){
  const w=toWorld(mx,my);
  let best=null,bd=1e9;
  for(const n of shownNodes()){
    const d=Math.hypot(n.x-w.x,n.y-w.y);
    const hit=Math.max(n.r+4/cam.k,12/cam.k);
    if(d<hit&&d<bd){bd=d;best=n;}
  }
  return best;
}
gcv.addEventListener('pointerdown',e=>{
  gcv.setPointerCapture(e.pointerId);
  downX=lastX=e.clientX;downY=lastY=e.clientY;moved=false;
  if(pickSat(...evXY(e))){camAnim=null;return;} /* satellites are not bodies */
  const n=pick(...evXY(e));
  if(n&&n.type!=='root'){drag=n;n.fx=n.x;n.fy=n.y;}
  else pan=true;
  camAnim=null;
});
gcv.addEventListener('pointermove',e=>{
  if(drag){
    if(Math.hypot(e.clientX-downX,e.clientY-downY)>4)moved=true;
    const w=toWorld(...evXY(e));
    drag.fx=w.x;drag.fy=w.y;reheat(.3);
    hideHC();
  }else if(pan){
    if(Math.hypot(e.clientX-downX,e.clientY-downY)>4)moved=true;
    cam.x-=(e.clientX-lastX)/cam.k;cam.y-=(e.clientY-lastY)/cam.k;
    lastX=e.clientX;lastY=e.clientY;
    hideHC();
  }else{
    const s=pickSat(...evXY(e));
    if(s){
      if(satHover!==s.key)satHover=s.key;
      hoverId=null;gcv.style.cursor='pointer';
      showSatHC(s,e.clientX,e.clientY);
      return;
    }
    satHover=null;
    const n=pick(...evXY(e));
    hoverId=n?n.id:null;
    gcv.style.cursor=n?'pointer':'default';
    if(n)showHC(n,e.clientX,e.clientY);else hideHC();
  }
});
let lastUp=0,clickT=null;
gcv.addEventListener('pointerup',e=>{
  if(drag){
    const d=drag;drag=null;
    if(d.type!=='root'&&d.id!==rootId){d.fx=null;d.fy=null;}
    /* the current root stays pinned where it was dropped */
  }
  pan=false;
  if(moved)return;
  const sat=pickSat(...evXY(e));
  if(sat){revealTodoRow(sat);return;}
  const n=pick(...evXY(e));
  const now=performance.now();
  if(now-lastUp<300){
    clearTimeout(clickT);clickT=null;lastUp=0;
    onDbl(n);return;
  }
  lastUp=now;
  clickT=setTimeout(()=>{clickT=null;onClick(n);},260);
});
function onClick(n){
  if(!n){clearFocus();clearPath();return;}
  clearPath();
  select(n.id,1);
}
function onDbl(n){
  if(!n)return;
  if(n.type==='group'){toggleGroup(n);return;}
  if(n.type==='hub'){
    const gs=GROUPS.filter(g=>g.cat===n.cat);
    const anyClosed=gs.some(g=>!expanded.get(g.id));
    gs.forEach(g=>setExp(g,anyClosed));reheat(.5);
    toast(anyClosed?`${CAT[n.cat].name} opened`:`${CAT[n.cat].name} collapsed`);
    return;
  }
  if(selId===n.id&&focusDepth===1){select(n.id,2);toast('Expanded to two degrees');}
  else select(n.id,2);
}
const PANEL_W=352;
function select(id,depth,fly=true){
  selId=id;focusDepth=depth;
  focusSet=neighborhood(id,depth);
  const n=byId.get(id);
  document.getElementById('crumb-name').textContent=n.label;
  document.getElementById('crumb-depth').textContent=`${depth} hop${depth>1?'s':''} · ${focusSet.size} nodes`;
  document.getElementById('crumb').classList.add('is-on');
  openPanel(n);
  syncSats(n);
  if(fly){
    const kT=Math.max(cam.k,1.6);
    const off=GW>760?PANEL_W/2/kT:0;
    flyTo(n.x+off,n.y,kT);
  }
}
function clearFocus(){
  selId=null;focusSet=null;focusDepth=0;
  document.getElementById('crumb').classList.remove('is-on');
  closePanel();
  clearSats();
}
function clearPath(){pathIds=null;pathEdges=null;}
function setExp(g,v){
  if(expanded.get(g.id)===v)return;
  expanded.set(g.id,v);
  if(v){
    const kids=LEAVES.filter(l=>l.group===g.id);
    kids.forEach((l,i)=>{
      const a=i/kids.length*Math.PI*2;
      l.x=g.x+Math.cos(a)*(18+(i%4)*9);l.y=g.y+Math.sin(a)*(18+(i%4)*9);
      l.vx=0;l.vy=0;
    });
  }
}
function toggleGroup(g){
  setExp(g,!expanded.get(g.id));reheat(.5);
  if(selId&&!isShown(byId.get(selId)))clearFocus();
  if(focusSet&&selId)focusSet=neighborhood(selId,focusDepth);
}
gcv.addEventListener('wheel',e=>{
  e.preventDefault();camAnim=null;
  const f=Math.exp(-e.deltaY*.0016);
  const nk=Math.max(.22,Math.min(5,cam.k*f));
  const [mx,my]=evXY(e);
  const w=toWorld(mx,my);
  cam.x=w.x-(mx-GW/2)/nk;
  cam.y=w.y-(my-GH/2)/nk;
  cam.k=nk;
},{passive:false});
document.getElementById('crumb-clear').addEventListener('click',()=>{clearFocus();clearPath();});

/* ============================== RE-ROOT ============================== */
const rootdd=document.getElementById('rootdd');
function computeDepths(rid){
  const m=new Map([[rid,0]]);
  let fr=[rid];
  while(fr.length){
    const nx=[];
    for(const u of fr)for(const{other}of byId.get(u).adj){
      if(!m.has(other)){m.set(other,m.get(u)+1);nx.push(other);}
    }
    fr=nx;
  }
  return m;
}
function setRoot(id){
  if(rootId===id){closeRootDD();return;}
  const old=byId.get(rootId);
  old.fx=null;old.fy=null;
  rootId=id;
  const r=byId.get(id);
  ensureShown(r);
  if(id===DATA.root.id){
    r.fx=0;r.fy=0;rootDepths=null;
  }else{
    r.fx=r.x;r.fy=r.y;rootDepths=computeDepths(id);
  }
  clearFocus();clearPath();
  document.getElementById('root-name').textContent=r.label;
  reheat(.8);
  go(graphRoute);
  flyTo(r.fx,r.fy,Math.min(Math.max(cam.k,.55),.9),900);
  toast(id===DATA.root.id?'Back to the full space':'Rooted on '+r.label);
  closeRootDD();
}
function closeRootDD(){rootdd.classList.remove('is-open');}
document.getElementById('root-btn').addEventListener('click',e=>{
  e.stopPropagation();
  rootdd.classList.toggle('is-open');
  if(rootdd.classList.contains('is-open')){
    const q=document.getElementById('root-q');
    q.value='';q.focus();
  }
});
document.getElementById('root-reset').addEventListener('click',()=>setRoot(DATA.root.id));
rootdd.addEventListener('click',e=>e.stopPropagation());
addEventListener('click',e=>{
  if(!rootdd.classList.contains('is-open'))return;
  if(!e.target.closest('.rootpick'))closeRootDD();
});

/* legend + counts */
{
  const lg=document.getElementById('legend');
  const glyph={
    disc:'<svg width="10" height="10"><circle cx="5" cy="5" r="3.6" fill="#b3ada0"/></svg>',
    ring:'<svg width="10" height="10"><circle cx="5" cy="5" r="3.1" fill="none" stroke="#b3ada0" stroke-width="1.4"/></svg>',
    diamond:'<svg width="10" height="10"><path d="M5 .9 9.1 5 5 9.1.9 5Z" fill="#b3ada0"/></svg>',
    stack:'<svg width="11" height="10"><rect x="1" y="3" width="6" height="6" fill="none" stroke="#b3ada0"/><rect x="4" y="1" width="6" height="6" fill="#b3ada0"/></svg>',
    slab:'<svg width="12" height="10"><rect x=".5" y="2.7" width="11" height="4.6" fill="#b3ada0"/></svg>'
  };
  const shapeDefs=DATA.meta.shapeLegend||[
    {shape:'disc',label:'code'},
    {shape:'ring',label:'document'},
    {shape:'diamond',label:'experiment'}
  ];
  const typeDefs=DATA.meta.typeLegend||[];
  lg.innerHTML=
    Object.values(CAT).map(c=>`<span class="li"><span class="sw" style="background:${c.color}"></span>${esc(c.name)}</span>`).join('')+
    shapeDefs.map((d,i)=>`<span class="li"${i===0?' style="margin-left:6px"':''}>${glyph[d.shape]||glyph.disc}${esc(d.label)}</span>`).join('')+
    typeDefs.map((d,i)=>`<span class="li${d.weight==='dim'?' li-dim':''}"${i===0?' style="margin-left:6px"':''}><span class="sw sw-ring"></span>${esc(d.label.toLowerCase())}</span>`).join('');
  document.getElementById('counts').textContent=
    `${LEAVES.length} ${noun} · ${GROUPS.length} ${collectionLabel} · ${EDGES.length} links · ${XCOUNT} cross-ties`;
}

/* ============================== HOVER CARD ============================== */
const hc=document.getElementById('hc');
function showHC(n,mx,my){
  const col=n.type==='root'?ACCENT_DEEP:CAT[n.cat].color;
  const kick=n.type==='hub'?hubLabel:n.type==='group'?'Cluster':n.type==='root'?'The center':`${CAT[n.cat].name} · ${n.tag}`;
  const art=`linear-gradient(155deg, ${hexA(col,.24)}, ${hexA(col,.03)} 68%)`;
  let rows='';
  if(n.type==='leaf'){
    rows=`<dl>
      ${n.date?`<dt>Born</dt><dd>${fmtDate(n.date)}</dd>`:''}
      <dt>Where</dt><dd class="mono">${esc(n.path)}</dd>
      <dt>Ties</dt><dd>${n.degree-1} connection${n.degree-1===1?'':'s'} · ${esc(byId.get(n.group).label)}</dd>
    </dl>`;
  }else if(n.type==='group'){
    const kids=LEAVES.filter(l=>belongsToCategory(l,n.cat));
    const dates=kids.map(x=>x.date).filter(Boolean).sort();
    const span=dates.length
      ?`<dt>Span</dt><dd>${fmtMY(+new Date(dates[0]))} to ${fmtMY(+new Date(dates.at(-1)))}</dd>`
      :'';
    rows=`<dl><dt>Holds</dt><dd>${kids.length} ${noun}</dd>${span}</dl>`;
  }else{
    const kids=n.type==='hub'?LEAVES.filter(l=>belongsToCategory(l,n.cat)):LEAVES;
    rows=`<dl><dt>Holds</dt><dd>${kids.length} ${noun}</dd></dl>`;
  }
  hc.innerHTML=`
    <div class="art" style="background:${art}">
      <div class="kicker">${esc(kick)}</div>
      <h5>${esc(n.label)}</h5>
      ${n.type==='leaf'?'':`<div class="sub">${esc((n.blurb||'').split('. ')[0])}</div>`}
    </div>
    ${rows}
    <div class="foot">${n.type==='group'?'Click to focus · Double-click to open or close':'Click to focus · Double-click to expand'}</div>`;
  placeHC(mx,my);
}
function placeHC(mx,my){
  hc.classList.add('is-on');
  const r=hc.getBoundingClientRect();
  let x=mx+18,y=my+18;
  if(x+r.width>innerWidth-8)x=mx-r.width-18;
  if(y+r.height>innerHeight-8)y=my-r.height-18;
  hc.style.left=Math.max(8,x)+'px';hc.style.top=Math.max(64,y)+'px';
}
function hideHC(){hc.classList.remove('is-on');hoverId=null;satHover=null;}

/* ============================== DETAIL PANEL ============================== */
const panel=document.getElementById('panel');
function openPanel(n){
  const col=n.type==='root'?ACCENT_DEEP:CAT[n.cat].color;
  const kick=n.type==='hub'?`${hubLabel} · ${LEAVES.filter(l=>belongsToCategory(l,n.cat)).length} ${noun}`
    :n.type==='group'?`${CAT[n.cat].name} · environment`
    :n.type==='root'?'The center'
    :`${CAT[n.cat].name} · ${n.tag}`;
  const conns=n.adj
    .filter(({other})=>byId.get(other).type!=='root'||n.type==='hub')
    .sort((p,q)=>(p.e.kind==='x'?0:1)-(q.e.kind==='x'?0:1))
    .slice(0,24)
    .map(({e,other})=>{
      const o=byId.get(other);
      let rel;
      if(e.kind==='x')rel=(e.s===n.id?'':'← ')+e.label;
      else rel=o.type==='group'||o.type==='hub'||o.type==='root'?'part of':'holds';
      return `<button class="conn" data-id="${o.id}">
        <span class="cdot" style="background:${o.type==='root'?ACCENT_DEEP:CAT[o.cat]?.color||'#e9e4d9'}"></span>
        <span>${esc(o.label)}</span>
        <span class="rel">${esc(rel)}</span>
        <span class="yr">${o.date?o.date.slice(0,7):''}</span>
      </button>`;
    }).join('');
  document.getElementById('panel-scroll').innerHTML=`
    <div class="poster" style="background:radial-gradient(120% 100% at 20% 0%, ${hexA(col,.20)}, transparent 62%)">
      <div class="kicker">${esc(kick)}</div>
      <h3>${esc(n.label)}</h3>
      ${n.date?`<div class="sub">${fmtDate(n.date)}</div>`:''}
      ${n.path?`<div class="path">${esc(n.path)}</div>`:''}
    </div>
    <div class="psec"><h4>About</h4><p>${esc(n.blurb||'')}</p></div>
    ${todoSectionHTML(n)}
    ${conns?`<div class="psec"><h4>Connections</h4>${conns}</div>`:''}
    <div class="pacts">
      ${n.type==='leaf'||n.type==='group'?`<button data-act="timeline">Show on timeline</button>`:''}
      ${previewable(n)?`<button data-act="preview">Preview file</button>`:''}
    </div>`;
  panel.classList.add('is-open');
  panel.dataset.id=n.id;
}
/* Only in the graph dataset: there a leaf is a file with a
   "<project>/<relative path>" path. A dashboard leaf is a whole project. */
const previewable=n=>bootDataset==='graph'&&n.type==='leaf'&&!!n.path&&n.path.includes('/');
function closePanel(){panel.classList.remove('is-open');}
document.getElementById('panel-close').addEventListener('click',()=>{clearFocus();clearPath();});
panel.addEventListener('click',e=>{
  const c=e.target.closest('.conn');
  if(c){
    const n=byId.get(c.dataset.id);
    ensureShown(n);
    go(graphRoute);
    select(n.id,1);
    pulseN={id:n.id,t0:performance.now()};
    return;
  }
  const row=e.target.closest('.ptodo');
  if(row){
    satPulse={key:row.dataset.todo,t0:performance.now()};
    return;
  }
  const a=e.target.closest('[data-act]');
  if(!a)return;
  const n=byId.get(panel.dataset.id);
  if(a.dataset.act==='timeline'){traceOnTimeline(n);return;}
  if(a.dataset.act==='preview'){
    const cut=n.path.indexOf('/');
    dispatchEvent(new CustomEvent('space:preview-file',{detail:{
      project:n.path.slice(0,cut),path:n.path.slice(cut+1),name:n.label}}));
    return;
  }
  if(a.dataset.act==='todos'){
    const pid=satHost;
    if(!pid)return;
    satCache.delete(pid);
    satHost=null;            /* defeat syncSats' same-project early return */
    syncSats(byId.get(pid));
  }
});
function ensureShown(n){
  if(n.type==='leaf'){
    if(!expanded.get(n.group)){setExp(byId.get(n.group),true);reheat(.4);}
  }
}

/* ============================== TODO SATELLITES ==============================
   Dashboard only: there a leaf IS an xo-project (leaf id == project id), so
   selecting one can show its live todos. In the Graph dataset leaves are files
   and the whole feature stays switched off.

   Satellites are UI state, never graph data: nothing is pushed into NODES /
   EDGES / byId / LEAVES. The model is built once from the dataset (:136-161)
   and half a dozen subsystems read it as a snapshot — LEAVES-derived counts,
   the timeline's lanes and axis, search, the re-root walk. A todo injected
   there would invent a timeline lane and inflate "N projects"; keeping them
   out of the model means none of those need to know this feature exists.
   The cost is that they get no adjacency, so hover/click/label are handled
   explicitly below. Positions are recomputed from the host each frame, so the
   constellation follows drags and settling for free without any force. */
const SAT_DOTS=28;    /* dots drawn — beyond this the orbit reads as noise */
const SAT_ROWS=40;    /* rows listed in the panel */
const SAT_TTL=20000;  /* ms a fetched list stays fresh (re-click is instant) */
const SAT_MIN_K=.55;  /* below this zoom, dots would collide with sibling nodes */
/* Same order the Files tab lists todos in (projects.js:28). Duplicated rather
   than imported: views never import each other (see the registry contract). */
const ST_ORDER={in_progress:0,pending:1,blocked:2,completed:3,cancelled:4};
const ST_DONE=new Set(['completed','cancelled']);
const ST_COLOR={in_progress:ACCENT,blocked:'#e0b04c',pending:'#b3ada0',
  completed:'#7d786d',cancelled:'#7d786d'};
let satHost=null;    /* project id the constellation belongs to, or null */
let satRows=[];      /* every todo, ST_ORDER-sorted — the panel's source */
let satDots=[];      /* satRows.slice(0,SAT_DOTS) — the canvas's source */
let satState='idle'; /* idle | loading | ready | error */
let satNote='';      /* one-line reason when state is error */
let satToken=0;      /* race guard: bumped whenever the selection changes */
let satT0=0;         /* grow-in start */
let satHover=null;   /* key of the hovered dot */
let satPulse=null;   /* {key,t0} — panel row clicked, flash its dot */
const satCache=new Map();

/* The one gate. Everything else early-returns on a null host. */
const todoProjectId=n=>bootDataset==='dashboard'&&n&&n.type==='leaf'?n.id:null;

function shapeTodos(res){
  if(!res.ok){
    return{rows:[],state:'error',
      note:res.notImplemented?'todos are not available for the active agent'
        :res.offline?'xo-space is unreachable':String(res.error||'could not read todos')};
  }
  const rows=[];
  for(const [sid,sess] of Object.entries(res.data.sessions||{})){
    for(const t of sess.todos||[]){
      rows.push({key:sid+'/'+t.id,sid,runtime:sess.runtime||'',
        status:t.status||'pending',content:t.content||'',
        st:ST_ORDER[t.status]??9});
    }
  }
  rows.sort((a,b)=>a.st-b.st||a.content.localeCompare(b.content));
  rows.forEach((r,i)=>{r.i=i;});
  return{rows,state:'ready',note:'',updated:res.data.updated_at||null};
}
function tallyText(){
  const by=new Map();
  for(const r of satRows)by.set(r.status,(by.get(r.status)||0)+1);
  return Object.keys(ST_ORDER)
    .filter(s=>by.get(s))
    .map(s=>`${by.get(s)} ${s.replace('_',' ')}`)
    .join(' · ').toUpperCase();
}

function syncSats(n){
  const pid=todoProjectId(n);
  if(pid&&pid===satHost)return; /* same project re-selected: keep what we have */
  clearSats();
  if(!pid)return;
  satHost=pid;satT0=performance.now();
  const hit=satCache.get(pid);
  if(hit&&performance.now()-hit.t<SAT_TTL){applySats(pid,hit,satToken);return;}
  satState='loading';renderTodoSection();
  loadSats(pid,satToken);
}
async function loadSats(pid,tok){
  const res=await apiFetch(API_BASE+'/api/xo-projects/'+encodeURIComponent(pid)+'/todos');
  if(tok!==satToken)return; /* a newer selection owns the screen */
  const shaped=shapeTodos(res);
  if(shaped.state==='ready'){
    if(satCache.size>40)satCache.clear();
    satCache.set(pid,{t:performance.now(),...shaped});
  }
  applySats(pid,shaped,tok);
}
function applySats(pid,shaped,tok){
  /* Two guards, not one: the token catches an A→B→A sequence where the host
     id alone would let A's older response paint over A's newer one. */
  if(tok!==satToken||satHost!==pid)return;
  satRows=shaped.rows;satDots=satRows.slice(0,SAT_DOTS);
  satState=shaped.state;satNote=shaped.note;
  renderTodoSection();
  reheat(.12); /* nudge the loop so the grow-in animates from a settled layout */
}
function clearSats(){
  satToken++;satHost=null;satRows=[];satDots=[];
  satState='idle';satNote='';satHover=null;satPulse=null;
}

/* Concentric shells at roughly constant arc spacing: 10, 16, 22, … */
function satSlot(i){
  let shell=0,base=0;
  for(;;){const cap=10+shell*6;if(i<base+cap)return{shell,slot:i-base,cap};base+=cap;shell++;}
}
/* The host must exist, be selected, and be on screen. A group can be collapsed
   out from under a live selection (double-clicking its hub, :654) — without
   this the constellation would hang in empty space. */
function satAnchor(){
  if(!satHost||!satDots.length)return null;
  const host=byId.get(satHost);
  return host&&isShown(host)?host:null;
}
function layoutSats(now){
  const host=satAnchor();
  if(!host)return;
  const grow=REDUCED?1:easeCubicInOut(Math.min(1,(now-satT0)/380));
  for(const s of satDots){
    const{shell,slot,cap}=satSlot(s.i);
    const a=slot/cap*Math.PI*2+shell*.31; /* fixed angle: hit-testing stays exact */
    const rr=(host.r+26+shell*19)*grow;
    s.x=host.x+Math.cos(a)*rr;s.y=host.y+Math.sin(a)*rr;
    s.r=s.status==='in_progress'?4.2:s.status==='blocked'?3.6:ST_DONE.has(s.status)?2.4:3.2;
  }
}
function drawSatOrbits(k){
  const host=satAnchor();
  if(!host||k<SAT_MIN_K)return;
  const col=CAT[host.cat]?.color||'#b3ada0';
  const shells=new Set(satDots.map(s=>satSlot(s.i).shell));
  for(const shell of shells){
    gc.beginPath();gc.arc(host.x,host.y,host.r+26+shell*19,0,Math.PI*2);
    gc.strokeStyle=hexA(col,.10);gc.lineWidth=.7/k;gc.stroke();
  }
  /* One spoke per in-progress todo only: 28 spokes is a starburst, but the
     handful of things actually in flight deserve a line back to the project. */
  for(const s of satDots){
    if(s.status!=='in_progress')continue;
    const dx=s.x-host.x,dy=s.y-host.y,d=Math.hypot(dx,dy)||1;
    gc.beginPath();
    gc.moveTo(host.x+dx/d*(host.r+5),host.y+dy/d*(host.r+5));
    gc.lineTo(s.x,s.y);
    gc.strokeStyle=hexA(ACCENT,.34);gc.lineWidth=.9/k;gc.stroke();
  }
}
function drawSatDots(now,k){
  const host=satAnchor();
  if(!host||k<SAT_MIN_K)return;
  for(const s of satDots){
    const col=ST_COLOR[s.status]||'#b3ada0';
    const done=ST_DONE.has(s.status);
    const on=s.key===satHover;
    if(s.status==='in_progress'&&!REDUCED){
      const b=.5+.5*Math.sin(now/520);
      gc.beginPath();gc.arc(s.x,s.y,s.r+2.6+b*1.4,0,Math.PI*2);
      gc.fillStyle=hexA(ACCENT,.10+b*.08);gc.fill();
    }
    gc.beginPath();gc.arc(s.x,s.y,s.r+(on?1.2:0),0,Math.PI*2);
    if(s.status==='pending'){
      gc.strokeStyle=hexA(col,on?.95:.6);gc.lineWidth=1.2/Math.sqrt(k);gc.stroke();
    }else{
      gc.fillStyle=hexA(col,done?.34:on?1:.88);gc.fill();
    }
    if(satPulse&&satPulse.key===s.key){
      const t=(now-satPulse.t0)/900;
      if(t>1)satPulse=null;
      else{
        gc.beginPath();gc.arc(s.x,s.y,s.r+2+t*16,0,Math.PI*2);
        gc.strokeStyle=hexA(ACCENT,(1-t)*.8);gc.lineWidth=1.6/Math.sqrt(k);gc.stroke();
      }
    }
  }
}
/* Screen space — called with the label transform already set. */
function drawSatLabels(k){
  const host=satAnchor();
  if(!host)return;
  const sx=(host.x-cam.x)*k+GW/2,sy=(host.y-cam.y)*k+GH/2;
  gc.font='400 8.5px '+MONO;
  const total=satRows.length;
  /* Clear of the outermost shell, not of the node: the caption sitting inside
     the orbit collides with the dots at the bottom of the constellation. */
  const shells=Math.max(...satDots.map(s=>satSlot(s.i).shell))+1;
  const out=(host.r+26+(shells-1)*19)*k+15;
  halo(`${total} TODO${total===1?'':'S'}`,sx,sy+out,'rgba(168,217,79,.9)',.14);
  const s=satDots.find(d=>d.key===satHover);
  if(!s||k<SAT_MIN_K)return;
  gc.font='400 10.5px '+SANS;
  const t=s.content.length>44?s.content.slice(0,43)+'…':s.content;
  halo(t,(s.x-cam.x)*k+GW/2,(s.y-cam.y)*k+GH/2-s.r*k-7,'rgba(233,228,217,.95)');
}
function pickSat(mx,my){
  const host=satAnchor();
  if(!host||cam.k<SAT_MIN_K)return null;
  const w=toWorld(mx,my);
  let best=null,bd=1e9;
  for(const s of satDots){
    const d=Math.hypot(s.x-w.x,s.y-w.y);
    /* Deliberately tighter than pick()'s 12/k floor: a generous satellite
       radius would swallow clicks meant for a neighbouring project. */
    const hit=Math.max(s.r+2.5/cam.k,7/cam.k);
    if(d<hit&&d<bd){bd=d;best=s;}
  }
  return best;
}
function showSatHC(s,mx,my){
  const col=ST_COLOR[s.status]||'#b3ada0';
  hc.innerHTML=`
    <div class="art" style="background:linear-gradient(155deg, ${hexA(col,.24)}, ${hexA(col,.03)} 68%)">
      <div class="kicker">Todo · ${esc(s.status.replace('_',' '))}</div>
      <h5>${esc(s.content)}</h5>
    </div>
    <dl><dt>Project</dt><dd>${esc(satHost||'')}</dd>
      ${s.runtime?`<dt>Runtime</dt><dd>${esc(s.runtime)}</dd>`:''}</dl>
    <div class="foot">Click to find it in the list</div>`;
  placeHC(mx,my);
}
/* Canvas → panel. The list is where the full text lives, so a dot click
   scrolls to its row and flashes it rather than opening anything new. */
function revealTodoRow(s){
  const row=panel.querySelector(`[data-todo="${CSS.escape(s.key)}"]`);
  if(!row)return;
  row.scrollIntoView({block:'nearest'});
  row.classList.add('is-flash');
  setTimeout(()=>row.classList.remove('is-flash'),900);
}

function todoSectionHTML(n){
  return todoProjectId(n)?`<div class="psec" id="panel-todos">${todoBodyHTML()}</div>`:'';
}
function todoBodyHTML(){
  const head=`<h4>Todos<button class="tref" data-act="todos" title="Re-fetch todos">&#8635;</button></h4>`;
  if(satState==='loading')return head+`<div class="prj-note">loading…</div>`;
  if(satState==='error')return head+`<div class="prj-note">${esc(satNote)}</div>`;
  if(!satRows.length)return head+`<div class="prj-note">no todos recorded yet</div>`;
  const shown=satRows.slice(0,SAT_ROWS);
  return head
    +`<div class="ptally">${esc(tallyText())}</div>`
    +`<div class="prj-todos">`+shown.map(t=>
      `<button class="prj-todo ptodo" data-todo="${esc(t.key)}">
        <span class="tchip st-${esc(t.status)}">${esc(t.status.replace('_',' '))}</span>
        <span class="tcontent${ST_DONE.has(t.status)?' done':''}">${esc(t.content)}</span>
        ${t.runtime?`<span class="truntime">${esc(t.runtime)}</span>`:''}
      </button>`).join('')+`</div>`
    +(satRows.length>shown.length?`<div class="prj-note">+${satRows.length-shown.length} more</div>`:'')
    +(satRows.length>SAT_DOTS?`<div class="prj-note">${SAT_DOTS} of ${satRows.length} shown on the map</div>`:'');
}
/* Patch only this subtree: re-rendering the whole panel would reset its
   scroll position and give the section two sources of truth. */
function renderTodoSection(){
  const el=document.getElementById('panel-todos');
  if(!el||panel.dataset.id!==satHost)return;
  el.innerHTML=todoBodyHTML();
}

/* ============================== SEARCH ============================== */
function rankMatches(q){
  q=q.trim().toLowerCase();
  if(!q)return[];
  const out=[];
  for(const n of NODES){
    if(n.type==='root')continue;
    const s=n.label.toLowerCase();
    let sc=-1;const idx=s.indexOf(q);
    if(idx===0)sc=0;
    else if(idx>0&&/\W/.test(s[idx-1]))sc=1;
    else if(idx>0)sc=2;
    else if((n.tag||'').toLowerCase().includes(q))sc=3;
    else if((n.blurb||'').toLowerCase().includes(q))sc=4;
    if(sc>=0)out.push([sc,n,idx]);
  }
  out.sort((a,b)=>a[0]-b[0]||(b[1].degree-a[1].degree)||a[1].label.length-b[1].label.length);
  return out.slice(0,8);
}
function acRow(n,idx,q){
  const col=n.cat?CAT[n.cat].color:'#e9e4d9';
  const name=idx>=0
    ?esc(n.label.slice(0,idx))+'<em>'+esc(n.label.slice(idx,idx+q.length))+'</em>'+esc(n.label.slice(idx+q.length))
    :esc(n.label);
  const meta=n.type==='hub'?'dept':n.type==='group'?'cluster':n.tag;
  const dia=n.shape==='diamond'?' dia':'';
  return {col,name,meta,dia};
}
function wireAC(input,acEl,onPick){
  let items=[],act=-1;
  const render=q=>{
    if(!items.length&&q){acEl.innerHTML=`<div class="empty">No match in this workspace<small>${LEAVES.length} ${noun} mapped</small></div>`;acEl.classList.add('is-open');return;}
    acEl.innerHTML=items.map(([sc,n,idx],i)=>{
      const r=acRow(n,idx,q);
      return `<button class="${i===act?'is-active':''}" data-i="${i}">
        <span class="tdot${r.dia}" style="background:${r.col}"></span><span>${r.name}</span><span class="meta">${esc(r.meta||'')}</span></button>`;
    }).join('');
    acEl.classList.toggle('is-open',items.length>0);
  };
  const pickI=i=>{
    if(i<0||i>=items.length)return;
    const n=items[i][1];
    acEl.classList.remove('is-open');items=[];act=-1;
    input.value=n.label;
    onPick(n);
  };
  input.addEventListener('input',()=>{items=rankMatches(input.value);act=items.length?0:-1;render(input.value.trim().toLowerCase());});
  input.addEventListener('keydown',e=>{
    if(e.key==='ArrowDown'){act=(act+1)%items.length;render(input.value.toLowerCase());e.preventDefault();}
    else if(e.key==='ArrowUp'){act=(act-1+items.length)%items.length;render(input.value.toLowerCase());e.preventDefault();}
    else if(e.key==='Enter'){pickI(act>=0?act:0);e.preventDefault();}
    else if(e.key==='Escape'){acEl.classList.remove('is-open');items=[];input.blur();}
  });
  input.addEventListener('blur',()=>setTimeout(()=>acEl.classList.remove('is-open'),140));
  acEl.addEventListener('pointerdown',e=>{
    const b=e.target.closest('button');
    if(b){e.preventDefault();pickI(+b.dataset.i);}
  });
}
wireAC(document.getElementById('q'),document.getElementById('qac'),n=>{
  ensureShown(n);
  go(graphRoute);
  clearPath();
  select(n.id,1,false);
  const kT=n.type==='leaf'?2.2:1.3;
  flyTo(n.x+(GW>760?PANEL_W/2/kT:0),n.y,kT);
  pulseN={id:n.id,t0:performance.now()};
  toast('Found '+n.label);
  document.getElementById('q').value='';
});
wireAC(document.getElementById('root-q'),document.getElementById('root-ac'),n=>setRoot(n.id));
document.getElementById('root-q').addEventListener('keydown',e=>{
  if(e.key==='Escape')closeRootDD();
});

/* ============================== VIEWS + GLOBAL KEYS ==============================
   Tab/section toggling now lives in core/registry.js. The atlas keeps only
   its internal notion of which of its lenses is active — it gates the sim
   loop and timeline rebuilds — plus the search-focus and clear keys. */
let view='graph';
/* List → Graph jump: focus the project's hub (graph dataset, `p_<id>`) or
   its project node (dashboard dataset, plain id). Unknown ids no-op. */
hooks.focusProject=()=>{
  if(!pendingFocus)return;
  const n=byId.get('p_'+pendingFocus)||byId.get(pendingFocus);
  pendingFocus=null;
  if(!n)return;
  ensureShown(n); /* a leaf handed over from the Tree lens may sit in a closed group */
  clearPath();
  select(n.id,1);
  pulseN={id:n.id,t0:performance.now()};
};
hooks.setActiveView=v=>{
  view=v;
  document.querySelectorAll('[data-atlas-lens]').forEach(button=>{
    button.classList.toggle('is-on',button.dataset.atlasLens===v);
  });
  hideHC();
  if(v==='graph'&&GW<50)resize(); /* booted while hidden (deep link): size the canvas now */
  if(v==='time'){requestAnimationFrame(()=>{buildTimeline();if(tTrace)drawTrace();});}
};
addEventListener('keydown',e=>{
  const typing=/INPUT|TEXTAREA/.test(document.activeElement?.tagName||'');
  if(e.key==='/'&&!typing){e.preventDefault();document.getElementById('q').focus();return;}
  if(typing)return;
  if(e.key==='Escape'){clearFocus();clearPath();hideHC();}
});

/* ============================== TIMELINE ============================== */
const T0G=+new Date(DATA.timeline.start+'T00:00:00'),T1G=+new Date(DATA.timeline.end+'T00:00:00');
/* TF0/TF1 is the full axis of the current mode (computeRange fits it to the
   data); T0/T1 is the window actually on screen — identical until the user
   zooms or picks a year. Everything downstream (yOf, the scrubber, Play, the
   ticks) reads the window, so zooming is nothing more than a narrower axis. */
let TF0=T0G,TF1=T1G;
let T0=T0G,T1=T1G;
let tZoomed=false;
const DAY=86400000,MIN_SPAN=DAY*7;
let laneFilter='';
let tRebuildRAF=null;
/* one rebuild per frame, however many wheel ticks arrive */
function scheduleBuild(){cancelAnimationFrame(tRebuildRAF);tRebuildRAF=requestAnimationFrame(buildTimeline);}
function setView(a,b){
  a=Math.max(TF0,a);b=Math.min(TF1,b);
  if(b-a<MIN_SPAN){const mid=(a+b)/2;a=Math.max(TF0,mid-MIN_SPAN/2);b=Math.min(TF1,a+MIN_SPAN);}
  T0=a;T1=b;
  tZoomed=!(a<=TF0&&b>=TF1);
  scheduleBuild();
}
function resetView(){tZoomed=false;T0=TF0;T1=TF1;scheduleBuild();}
/* year chips: one per year the full axis touches, plus All once zoomed */
function renderYears(){
  const el=document.getElementById('tyears');if(!el)return;
  const y0=new Date(TF0).getFullYear(),y1=new Date(TF1).getFullYear();
  /* a year earns a chip only if the axis spends at least MIN_SPAN in it:
     the 7-day pad alone must not grow a chip whose window then has to be
     re-widened across the boundary and can never light up */
  const years=[];
  for(let y=y0;y<=y1;y++){
    const overlap=Math.min(TF1,+new Date(y+1,0,1))-Math.max(TF0,+new Date(y,0,1));
    if(overlap>=MIN_SPAN)years.push(y);
  }
  const inYear=y=>tZoomed&&T0>=+new Date(y,0,1)-DAY&&T1<=+new Date(y+1,0,1)+DAY;
  const many=years.length>1;
  el.innerHTML=(many||tZoomed
      ?`<button type="button" data-year="all"${tZoomed?'':' class="is-on"'}>All</button>`:'')
    +(many?years.map(y=>`<button type="button" data-year="${y}"${inYear(y)?' class="is-on"':''}>${y}</button>`).join(''):'');
  el.hidden=!many&&!tZoomed;
}
document.getElementById('tyears').addEventListener('click',e=>{
  const b=e.target.closest('[data-year]');if(!b)return;
  stopPlay();
  if(b.dataset.year==='all'){resetView();return;}
  const y=+b.dataset.year;
  setView(+new Date(y,0,1),+new Date(y+1,0,1));
});
document.getElementById('tlanes').addEventListener('input',e=>{
  laneFilter=e.target.value;scheduleBuild();
});
const SVGNS='http://www.w3.org/2000/svg';
let tNow=T1G,tPlaying=false,tTrace=null;
const tplot=document.getElementById('tplot');
const tsvg=document.createElementNS(SVGNS,'svg');
tplot.appendChild(tsvg);
const MILES=DATA.milestones;
/* Two modes over one axis: 'file' plots every dated artifact as a beeswarm;
   'project' plots each project's git commit history in parallel lanes (one
   dot per day, sized by commits). The mode survives reloads; the toggle only
   appears when the dataset carries git history (the Dashboard projection
   and static fallback files do not). */
const GITHIST=DATA.gitHistory||{};
const histLanes=Object.keys(CAT).filter(cat=>(GITHIST[cat]||[]).length);
const hasHist=histLanes.length>0;
/* Both modes plot git dates only, so a project with no repository has no
   lane at all. Files counts every project; without this note the Timeline
   silently shows fewer and reads as broken data rather than as the absence
   of git history it actually is. */
const fileLanes=()=>Object.keys(CAT).filter(cat=>LEAVES.some(n=>n.cat===cat&&n.date));
function coverageNote(){
  const total=Object.keys(CAT).length;
  const shown=(tMode==='project'?histLanes:fileLanes()).length;
  const blank=total-shown;
  if(!total||blank<=0)return'';
  /* Every project has a lane now, so this counts the empty ones rather than
     claiming a subset is "shown" — the dark columns are visible evidence. */
  return ` ${blank} of ${total} project${total===1?'':'s'} ${blank===1?'has':'have'} no git history to plot; their lanes are dark.`;
}
const TMODE_KEY='space.timelineMode';
let tMode='file';
try{if(localStorage.getItem(TMODE_KEY)==='project'&&hasHist)tMode='project';}catch(_err){}
let histDots=[];
{
  const tmodeEl=document.getElementById('tmode');
  if(tmodeEl&&hasHist)tmodeEl.hidden=false;
}
document.querySelectorAll('#tmode [data-tmode]').forEach(button=>{
  button.addEventListener('click',()=>setTMode(button.dataset.tmode));
});
function defaultSub(){
  if(tMode==='project'){
    return'Every project’s git history in parallel · newest at the top · dot size = commits that day.'
      +coverageNote();
  }
  return(DATA.meta.timelineSub||
    'Scrub through the workspace as it grew, newest at the top. Open any cluster from the graph to watch its run unfold here.')
    /* the two modes fit their own data, so this axis is usually the shorter
       one; say so, or the mismatch reads as missing history */
    +(hasHist?' Files plot their git dates only, so this axis is shorter than By project.':'')
    +coverageNote();
}
function syncTModeUI(){
  document.querySelectorAll('#tmode [data-tmode]').forEach(button=>{
    button.classList.toggle('is-on',button.dataset.tmode===tMode);
  });
  if(!tTrace)document.getElementById('tsub').textContent=defaultSub();
}
function setTMode(mode){
  if(mode===tMode||(mode==='project'&&!hasHist))return;
  tMode=mode;
  try{localStorage.setItem(TMODE_KEY,mode);}catch(_err){}
  hideHC();
  if(mode==='project'&&tTrace)clearTrace(); /* traces are a By-file tool */
  syncTModeUI();
  buildTimeline();
}
syncTModeUI();
/* Each mode gets its own axis, spanning only what it actually plots: the
   file plot spans the kept leaves' git dates, the project plot spans commit
   history. One shared axis would leave Play sweeping months of empty space
   whenever one mode's data starts far earlier than the other's. */
function computeRange(){
  const stamps=tMode==='project'
    ?histLanes.flatMap(cat=>(GITHIST[cat]||[]).map(day=>+new Date(day.d+'T00:00:00')))
    :LEAVES.filter(n=>n.date).map(n=>+new Date(n.date+'T00:00:00'));
  if(!stamps.length){TF0=T0G;TF1=T1G;}
  else{
    const pad=86400000*7;
    let lo=Infinity,hi=-Infinity;
    for(const t of stamps){if(t<lo)lo=t;if(t>hi)hi=t;}
    TF0=lo-pad;TF1=hi+pad;
  }
  /* a zoomed window survives a mode switch as long as it still fits the
     new axis; otherwise fall back to the full range */
  if(tZoomed){
    T0=Math.max(TF0,T0);T1=Math.min(TF1,T1);
    if(T1-T0<MIN_SPAN)tZoomed=false;
  }
  if(!tZoomed){T0=TF0;T1=TF1;}
  tNow=Math.min(Math.max(tNow,T0),T1);
  renderYears();
  const ticks=document.querySelector('#view-time .ticks');
  if(ticks){
    const fmtTick=(t,withYear)=>new Date(t).toLocaleDateString('en-US',
      withYear?{month:'short',year:'numeric'}:{month:'short'}).toUpperCase();
    ticks.innerHTML=[0,.25,.5,.75,1].map((f,i)=>
      `<span>${fmtTick(T0+f*(T1-T0),i===0||i===4)}</span>`).join('');
  }
}
function buildTimeline(){
  const W=tplot.clientWidth,H=tplot.clientHeight;
  if(W<50||H<50)return;
  computeRange();
  histDots=[];
  /* Every project gets a lane, including the ones with nothing to plot.
     Dropping them made the Timeline disagree with Files about how many
     projects exist, and a reader cannot tell "no history" from "missing".
     An empty lane is drawn dark and labelled instead. */
  const allLanes=Object.keys(CAT);
  const hasData=cat=>tMode==='project'
    ?(GITHIST[cat]||[]).length>0
    :LEAVES.some(n=>n.cat===cat&&n.date);
  /* The lane filter narrows by project name: a hundred lanes at 18px each
     is a smear, not a chart. Below MIN_COL the plot stops squeezing and
     grows wider than the pane instead, panning sideways (drag, shift+wheel)
     so the column headers stay legible whatever the project count. */
  const q=laneFilter.trim().toLowerCase();
  const lanes=q?allLanes.filter(cat=>CAT[cat].name.toLowerCase().includes(q)):allLanes;
  const laneSet=new Set(lanes);
  const MIN_COL=100;
  const colW=Math.max(MIN_COL,(W-64-16)/Math.max(1,lanes.length));
  const SW=Math.max(W,Math.round(64+16+colW*lanes.length));
  tsvg.setAttribute('viewBox',`0 0 ${SW} ${H}`);
  tsvg.style.width=SW+'px';
  tsvg.innerHTML='';
  if(!lanes.length){
    const none=document.createElementNS(SVGNS,'text');
    none.setAttribute('x',SW/2);none.setAttribute('y',H/2);
    none.setAttribute('text-anchor','middle');
    none.setAttribute('style',`font:italic 400 13px ${SERIF};fill:#56534b`);
    none.textContent='No project matches the filter.';
    tsvg.appendChild(none);
  }
  /* Time runs vertically: newest at the top, oldest at the bottom. Narrow
     columns rotate their headers, which needs a taller top margin. */
  const rotated=colW<64;
  const M={t:rotated?76:34,r:16,b:18,l:64};
  /* no plot band, no plot: with the margins eating the whole height yOf
     divides by zero and a wheel tick would turn the window into NaN */
  if(H<M.t+M.b+20){tsvg._yOf=null;return;}
  /* Three layers, bottom to top: bands and grid, then the data clipped to
     the plot band (a dot on the first row cannot spill into the headers,
     one on the last cannot bury the commit counts), then the labels with
     a dark halo. Zoom changes what the data does; the label zones stay
     reserved regardless. */
  const defs=document.createElementNS(SVGNS,'defs');
  const clip=document.createElementNS(SVGNS,'clipPath');
  clip.setAttribute('id','tclip');
  const clipRect=document.createElementNS(SVGNS,'rect');
  clipRect.setAttribute('x',0);clipRect.setAttribute('y',M.t-6);
  clipRect.setAttribute('width',SW);clipRect.setAttribute('height',H-M.t-M.b+12);
  clip.appendChild(clipRect);defs.appendChild(clip);tsvg.appendChild(defs);
  const plotG=document.createElementNS(SVGNS,'g');
  plotG.setAttribute('clip-path','url(#tclip)');
  const labelsG=document.createElementNS(SVGNS,'g');
  labelsG.setAttribute('style','paint-order:stroke;stroke:rgba(11,13,16,.9);stroke-width:3px;stroke-linejoin:round');
  const yOf=t=>M.t+(T1-t)/(T1-T0)*(H-M.t-M.b);
  /* column bands + headers */
  lanes.forEach((cat,i)=>{
    const x=M.l+i*colW;
    const band=document.createElementNS(SVGNS,'rect');
    band.setAttribute('x',x+2);band.setAttribute('y',M.t-6);
    band.setAttribute('width',Math.max(2,colW-4));band.setAttribute('height',H-M.t-M.b+12);
    const live=hasData(cat);
    /* darker than the workspace background, so an empty lane reads as a
       deliberate blank rather than as a gap in the layout */
    band.setAttribute('fill',live?hexA(CAT[cat].color,.04):'rgba(0,0,0,.22)');
    band.setAttribute('rx',8);
    if(!live){
      band.setAttribute('stroke','rgba(233,228,217,.05)');
      band.setAttribute('stroke-dasharray','2 4');
    }
    tsvg.appendChild(band);
    const name=CAT[cat].name;
    /* fit the label to the lane, not to a fixed count: an 18-character
       name is ~125px upright and collided with its neighbours */
    const maxChars=rotated?18:Math.max(4,Math.floor((colW-8)/7.2));
    const label=name.length>maxChars?name.slice(0,Math.max(1,maxChars-1))+'…':name;
    const lb=document.createElementNS(SVGNS,'text');
    const full=document.createElementNS(SVGNS,'title');
    full.textContent=name;lb.appendChild(full);
    if(rotated){
      const ax=x+colW/2+4,ay=M.t-10;
      lb.setAttribute('x',ax);lb.setAttribute('y',ay);
      lb.setAttribute('text-anchor','start');
      lb.setAttribute('transform',`rotate(-40 ${ax} ${ay})`);
      lb.setAttribute('style',`font:italic 500 10.5px ${SERIF};fill:${live?hexA(CAT[cat].color,.95):'rgba(125,120,109,.85)'}`);
    }else{
      lb.setAttribute('x',x+colW/2);lb.setAttribute('y',M.t-14);
      lb.setAttribute('text-anchor','middle');
      lb.setAttribute('style',`font:italic 500 13px ${SERIF};fill:${live?hexA(CAT[cat].color,.95):'rgba(125,120,109,.85)'}`);
    }
    lb.appendChild(document.createTextNode(label));
    labelsG.appendChild(lb);
    if(!live){
      /* one line, centred in the empty column, saying why it is empty */
      const why=document.createElementNS(SVGNS,'text');
      why.setAttribute('x',x+colW/2);why.setAttribute('y',(M.t+H-M.b)/2);
      why.setAttribute('text-anchor','middle');
      why.setAttribute('style',`font:400 8.5px ${MONO};letter-spacing:.1em;fill:#56534b`);
      why.textContent=colW>=104?'NO GIT HISTORY':colW>=64?'NO HISTORY':'—';
      labelsG.appendChild(why);
    }
    if(tMode==='project'&&live){
      const total=(GITHIST[cat]||[]).reduce((sum,day)=>sum+day.n,0);
      const sub=document.createElementNS(SVGNS,'text');
      sub.setAttribute('x',x+colW/2);sub.setAttribute('y',H-4);
      sub.setAttribute('text-anchor','middle');
      sub.setAttribute('style',`font:400 8.5px ${MONO};letter-spacing:.06em;fill:#56534b`);
      sub.textContent=colW>=70?`${total} COMMIT${total===1?'':'S'}`:String(total);
      labelsG.appendChild(sub);
    }
  });
  /* month grid: horizontal rules, labeled in the left margin */
  {
    const monthCount=Math.max(1,Math.round((T1-T0)/2592000000));
    const labelEvery=monthCount>26?3:1;
    let d=new Date(T0),mi=0;
    while(+d<T1){
      const y=yOf(+d);
      const ln=document.createElementNS(SVGNS,'line');
      ln.setAttribute('x1',M.l-6);ln.setAttribute('x2',SW-M.r);
      ln.setAttribute('y1',y);ln.setAttribute('y2',y);
      ln.setAttribute('stroke',d.getMonth()===0?'rgba(233,228,217,.13)':'rgba(233,228,217,.05)');
      ln.setAttribute('stroke-dasharray','1 4');
      tsvg.appendChild(ln);
      if(mi%labelEvery===0){
        const tx=document.createElementNS(SVGNS,'text');
        tx.setAttribute('x',M.l-12);tx.setAttribute('y',y+3);
        tx.setAttribute('text-anchor','end');
        tx.setAttribute('style',`font:400 8.5px ${MONO};letter-spacing:.08em;fill:#56534b`);
        const opts=d.getMonth()===0?{month:'short',year:'2-digit'}:{month:'short'};
        tx.textContent=d.toLocaleDateString('en-US',opts).toUpperCase();
        tsvg.appendChild(tx);
      }
      mi++;
      d=new Date(d.getFullYear(),d.getMonth()+1,1);
    }
  }
  /* milestone pips: far-left margin, at their moment in time */
  MILES.forEach(m=>{
    const t=+new Date(m.d+'T00:00:00');
    if(t<T0||t>T1)return; /* outside this mode's axis */
    const y=yOf(t);
    const c=document.createElementNS(SVGNS,'circle');
    c.setAttribute('cx',8);c.setAttribute('cy',y);c.setAttribute('r',2.4);
    c.setAttribute('fill','#3a4136');c.dataset.milestone='1';c.dataset.t=+new Date(m.d+'T00:00:00');
    tsvg.appendChild(c);
  });
  tsvg.appendChild(plotG);
  if(tMode==='file'){
  /* beeswarm: the date sets the row (y); collisions fan sideways inside the
     column, spilling downward (older) when a column is packed */
  lanes.forEach((cat,li)=>{
    const ns=LEAVES.filter(n=>n.cat===cat&&n.date).sort((a,b)=>a.date<b.date?-1:1);
    const placed=[];
    const baseX=M.l+li*colW+colW/2;
    const maxRow=Math.max(1,Math.floor((colW/2-5)/9));
    ns.forEach(n=>{
      n.ty=yOf(+new Date(n.date+'T00:00:00'));
      const hit=r=>placed.some(p=>Math.abs(p.ty-n.ty)<12&&p.row===r);
      let row=0,guard=0;
      while(hit(row)&&guard++<200){
        row=row>0?-row:-row+1;
        if(Math.abs(row)>maxRow){n.ty+=10;row=0;}
      }
      n.row=row;n.tx=baseX+row*9;
      placed.push(n);
    });
  });
  const dotsG=document.createElementNS(SVGNS,'g');
  dotsG.setAttribute('id','tdots');
  plotG.appendChild(dotsG);
  LEAVES.forEach(n=>{
    /* git-dated artifacts only, and only in lanes that are on the plot: a
       filtered-out leaf has no position and would land on stale coordinates */
    if(!n.date||!laneSet.has(n.cat)){n.tEl=null;return;}
    const col=CAT[n.cat].color;
    const r=3.2+Math.min(2.6,(n.degree-1)*.5);
    let el;
    if(n.shape==='diamond'){
      el=document.createElementNS(SVGNS,'rect');
      const s=r*1.6;
      el.setAttribute('x',n.tx-s/2);el.setAttribute('y',n.ty-s/2);
      el.setAttribute('width',s);el.setAttribute('height',s);
      el.setAttribute('transform',`rotate(45 ${n.tx} ${n.ty})`);
      el.setAttribute('fill',col);
    }else if(n.shape==='ring'){
      el=document.createElementNS(SVGNS,'circle');
      el.setAttribute('cx',n.tx);el.setAttribute('cy',n.ty);el.setAttribute('r',r-.5);
      el.setAttribute('fill','none');el.setAttribute('stroke',col);el.setAttribute('stroke-width',1.5);
    }else{
      el=document.createElementNS(SVGNS,'circle');
      el.setAttribute('cx',n.tx);el.setAttribute('cy',n.ty);el.setAttribute('r',r);
      el.setAttribute('fill',col);
    }
    el.dataset.id=n.id;
    el.style.cursor='pointer';
    dotsG.appendChild(el);
    n.tEl=el;
  });
  /* trace layer */
  const traceG=document.createElementNS(SVGNS,'g');
  traceG.setAttribute('id','ttrace');
  plotG.insertBefore(traceG,dotsG);
  }else{
  /* parallel git histories: one column per project, one dot per commit-day.
     Radius grows with the square root of that day's commit count, capped so
     tight column packing never bleeds across bands. */
  lanes.forEach((cat,li)=>{
    const baseX=M.l+li*colW+colW/2;
    const col=CAT[cat].color;
    const base=document.createElementNS(SVGNS,'line');
    base.setAttribute('x1',baseX);base.setAttribute('x2',baseX);
    base.setAttribute('y1',M.t-6);base.setAttribute('y2',H-M.b+6);
    base.setAttribute('stroke',hexA(col,.18));base.setAttribute('stroke-width',1);
    plotG.appendChild(base);
    (GITHIST[cat]||[]).forEach(day=>{
      const t=+new Date(day.d+'T00:00:00');
      const dot=document.createElementNS(SVGNS,'circle');
      const r=Math.max(2,Math.min(colW*.42,2+Math.sqrt(day.n)*1.6));
      dot.setAttribute('cx',baseX);dot.setAttribute('cy',yOf(t));
      dot.setAttribute('r',r);dot.setAttribute('fill',col);
      dot.dataset.hist=String(histDots.length);
      dot.style.cursor='pointer';
      plotG.appendChild(dot);
      histDots.push({el:dot,t,cat,day});
    });
  });
  }
  /* sweep: a horizontal rule at the scrubbed moment */
  const sweep=document.createElementNS(SVGNS,'line');
  sweep.setAttribute('id','tsweep');
  sweep.setAttribute('x1',M.l-6);sweep.setAttribute('x2',SW-M.r);
  sweep.setAttribute('stroke',ACCENT);sweep.setAttribute('stroke-width',1.2);
  sweep.setAttribute('stroke-dasharray','2 4');sweep.setAttribute('opacity',.55);
  tsvg.appendChild(sweep);
  tsvg.appendChild(labelsG);
  tsvg._yOf=yOf;
  /* "after a rebuild, the trace is back" is the rebuild's invariant, not
     the callers' — zoom, pan, chips, filter and resize all rebuild */
  if(tTrace&&tMode==='file')drawTrace();else renderTimelineState();
}
function renderTimelineState(){
  const yOf=tsvg._yOf;if(!yOf)return;
  document.getElementById('tsweep')?.setAttribute('y1',yOf(tNow));
  document.getElementById('tsweep')?.setAttribute('y2',yOf(tNow));
  if(tMode==='project'){
    histDots.forEach(d=>d.el.setAttribute('opacity',d.t<=tNow?.85:.08));
  }else LEAVES.forEach(n=>{
    if(!n.tEl)return;
    const born=+new Date(n.date+'T00:00:00')<=tNow;
    let op=born?.8:.06;
    if(tTrace){
      const inTrace=tTrace.ids.has(n.id);
      op=inTrace?(born?1:.06):(born?.10:.03);
    }
    n.tEl.setAttribute('opacity',op);
  });
  tsvg.querySelectorAll('[data-milestone]').forEach(c=>{
    c.setAttribute('fill',+c.dataset.t<=tNow?ACCENT_DEEP:'#33362f');
  });
  /* readout + milestone caption */
  document.getElementById('treadout').textContent=fmtMY(tNow);
  const m=[...MILES].reverse().find(x=>+new Date(x.d+'T00:00:00')<=tNow);
  const mEl=document.getElementById('tmilestone');
  /* labelled as what it is: a bare project name here read as a stray file */
  mEl.textContent=m?'◆ milestone · '+m.t:'';
  mEl.style.opacity=m?1:0;
  document.getElementById('tscrub').value=Math.round((tNow-T0)/(T1-T0)*1000);
}
function traceOnTimeline(n){
  if(tMode!=='file')setTMode('file'); /* traces live on the By-file plot */
  const ids=n.type==='group'||n.type==='hub'
    ?LEAVES.filter(l=>belongsToCategory(l,n.cat)):[n];
  const list=ids.filter(l=>l.date).sort((a,b)=>a.date<b.date?-1:1);
  tTrace={ids:new Set(list.map(x=>x.id)),list,label:n.label};
  /* a trace plays from its first date; a zoomed window that excludes it
     would show nothing until the user found the All chip */
  if(tZoomed)resetView();
  go('time');
  requestAnimationFrame(()=>{
    if(!list.length){
      document.getElementById('tsub').textContent=`${n.label} has no git-dated ${noun} to trace.`;
      document.getElementById('tclear').hidden=false;
      return;
    }
    drawTrace();
    document.getElementById('tclear').hidden=false;
    const m0=fmtMY(+new Date(list[0].date)),m1=fmtMY(+new Date(list[list.length-1].date));
    document.getElementById('tsub').textContent=
      `${n.label}: ${list.length} ${noun}, ${m0===m1?m0:m0+' to '+m1}.`;
    if(!REDUCED){
      tNow=+new Date(list[0].date+'T00:00:00')-86400000*7;
      startPlay();
    }else renderTimelineState();
  });
}
function drawTrace(){
  const g=tsvg.querySelector('#ttrace');
  if(!g)return;
  g.innerHTML='';
  /* only the leaves that made it onto the plot — the lane filter may hide some */
  const shown=tTrace?tTrace.list.filter(n=>n.tEl):[];
  if(shown.length<2){renderTimelineState();return;}
  const pts=shown.map(n=>[n.tx,n.ty]);
  let path=`M ${pts[0][0]} ${pts[0][1]}`;
  for(let i=1;i<pts.length;i++){
    const [x0,y0]=pts[i-1],[x1,y1]=pts[i];
    const my=(y0+y1)/2;
    path+=` C ${x0} ${my}, ${x1} ${my}, ${x1} ${y1}`;
  }
  const p=document.createElementNS(SVGNS,'path');
  p.setAttribute('d',path);p.setAttribute('fill','none');
  p.setAttribute('stroke',ACCENT);p.setAttribute('stroke-width',1.3);p.setAttribute('opacity',.7);
  g.appendChild(p);
  /* labels: alternate left/right, and step outward when several share a y window */
  const win=[];
  shown.forEach((n,i)=>{
    const near=win.filter(w=>Math.abs(w-n.ty)<24).length;
    win.push(n.ty);
    const left=i%2===0;
    const step=Math.floor(near/2)*11;
    const t=document.createElementNS(SVGNS,'text');
    t.setAttribute('x',left?n.tx-10-step:n.tx+10+step);
    t.setAttribute('y',n.ty+3);
    t.setAttribute('text-anchor',left?'end':'start');
    t.setAttribute('style',`font:400 9.5px ${SERIF};fill:#b3ada0`);
    t.textContent=n.label;
    g.appendChild(t);
  });
  renderTimelineState();
}
function clearTrace(){
  tTrace=null;
  document.getElementById('tclear').hidden=true;
  document.getElementById('tsub').textContent=defaultSub();
  const g=tsvg.querySelector('#ttrace');if(g)g.innerHTML='';
  renderTimelineState();
}
document.getElementById('tclear').addEventListener('click',clearTrace);
document.getElementById('tscrub').addEventListener('input',e=>{
  stopPlay();
  tNow=T0+(+e.target.value/1000)*(T1-T0);
  renderTimelineState();
});
let playRAF=null;
function startPlay(){
  tPlaying=true;
  document.querySelector('#tplay span').textContent='Pause';
  const step=()=>{
    tNow+=(T1-T0)/(60*16);
    if(tNow>=T1){tNow=T1;stopPlay();}
    renderTimelineState();
    if(tPlaying)playRAF=requestAnimationFrame(step);
  };
  cancelAnimationFrame(playRAF);playRAF=requestAnimationFrame(step);
}
function stopPlay(){
  tPlaying=false;cancelAnimationFrame(playRAF);
  document.querySelector('#tplay span').textContent='Play';
}
document.getElementById('tplay').addEventListener('click',()=>{
  if(tPlaying){stopPlay();return;}
  if(tNow>=T1-3600000)tNow=T0;
  startPlay();
});
function showHistHC(d,mx,my){
  if(!d)return;
  const col=CAT[d.cat].color;
  const subjects=(d.day.s||[]).map(s=>`<dt>·</dt><dd>${esc(s)}</dd>`).join('');
  hc.innerHTML=`
    <div class="art" style="background:linear-gradient(155deg, ${hexA(col,.24)}, ${hexA(col,.03)} 68%)">
      <div class="kicker">${esc(CAT[d.cat].name)} · git</div>
      <h5>${fmtDate(d.day.d)}</h5>
      <div class="sub">${d.day.n} commit${d.day.n===1?'':'s'} this day</div>
    </div>
    ${subjects?`<dl>${subjects}</dl>`:''}
    <div class="foot">Click to open this project on the graph</div>`;
  placeHC(mx,my);
}
/* Axis zoom + pan. Wheel zooms the time axis around the moment under the
   cursor; a vertical drag pans it; a horizontal drag (or shift+wheel) pans
   the lanes when they are wider than the pane. The same gestures as the
   Graph and Tree lenses, so a hand that knows one knows all three. */
const tOfY=y=>{
  const yOf=tsvg._yOf;if(!yOf)return T1;
  const top=yOf(T1),bottom=yOf(T0);
  return T1-(y-top)/(bottom-top)*(T1-T0);
};
tplot.addEventListener('wheel',e=>{
  e.preventDefault();
  if(e.shiftKey||(e.deltaX&&!e.deltaY)){tplot.scrollLeft+=e.deltaX||e.deltaY;return;}
  stopPlay();
  const r=tplot.getBoundingClientRect();
  const t=tOfY(e.clientY-r.top);
  const f=Math.exp(e.deltaY*.0016);
  setView(t-(t-T0)*f,t+(T1-t)*f);
},{passive:false});
let tDrag=null,tDragMoved=false;
tplot.addEventListener('pointerdown',e=>{
  if(e.button!==0||tDrag)return;
  tDrag={id:e.pointerId,x:e.clientX,y:e.clientY,x0:e.clientX,y0:e.clientY};tDragMoved=false;
});
tplot.addEventListener('pointermove',e=>{
  if(!tDrag||e.pointerId!==tDrag.id)return;
  /* a release the pane never saw (before capture, outside it) must not
     leave a phantom drag that pans on the next un-pressed hover */
  if(e.buttons===0){endDrag();return;}
  if(!tDragMoved){
    /* 4px of slack keeps a click on a dot a click; capture only once the
       drag is real, or the captured pointer would retarget that click */
    if(Math.hypot(e.clientX-tDrag.x0,e.clientY-tDrag.y0)<=4)return;
    tDragMoved=true;
    tplot.setPointerCapture(e.pointerId);tplot.classList.add('is-panning');
    stopPlay();hideHC();
  }
  const dx=e.clientX-tDrag.x,dy=e.clientY-tDrag.y;
  tDrag.x=e.clientX;tDrag.y=e.clientY;
  tplot.scrollLeft-=dx;
  const yOf=tsvg._yOf;
  if(yOf&&dy){
    const span=T1-T0,hp=yOf(T0)-yOf(T1);
    /* the window slides with the pointer but never past the full axis */
    const a=Math.max(TF0,Math.min(T0+dy*span/hp,TF1-span));
    T0=a;T1=a+span;tZoomed=!(T0<=TF0&&T1>=TF1);
    scheduleBuild();
  }
});
function endDrag(e){
  if(!tDrag||(e&&e.pointerId!==undefined&&e.pointerId!==tDrag.id))return;
  tDrag=null;tplot.classList.remove('is-panning');
}
/* on window, not the pane: an uncaptured release lands wherever the
   pointer is, and the pane only captures once a drag is real */
addEventListener('pointerup',endDrag);
addEventListener('pointercancel',endDrag);
tsvg.addEventListener('pointermove',e=>{
  if(tDrag&&tDragMoved)return;
  const t=e.target;
  if(t.dataset&&t.dataset.id){showHC(byId.get(t.dataset.id),e.clientX,e.clientY);}
  else if(t.dataset&&t.dataset.hist){showHistHC(histDots[+t.dataset.hist],e.clientX,e.clientY);}
  else hideHC();
});
tsvg.addEventListener('pointerleave',hideHC);
tsvg.addEventListener('click',e=>{
  /* a drag that ended on a dot is a pan, not a click */
  if(tDragMoved){tDragMoved=false;return;}
  const t=e.target;
  if(t.dataset&&t.dataset.id){
    const n=byId.get(t.dataset.id);
    ensureShown(n);
    go(graphRoute);
    select(n.id,1);
    pulseN={id:n.id,t0:performance.now()};
  }else if(t.dataset&&t.dataset.hist){
    /* a commit dot names its project: jump to that hub on the graph */
    const d=histDots[+t.dataset.hist];
    if(!d||!byId.get(d.cat))return;
    go(graphRoute);
    select(d.cat,1);
    pulseN={id:d.cat,t0:performance.now()};
  }
});

/* ============================== BOOT ============================== */
function resize(){
  dpr=Math.min(2,devicePixelRatio||1);
  const r=document.getElementById('view-graph').getBoundingClientRect();
  GW=r.width;GH=r.height;
  gcv.width=GW*dpr;gcv.height=GH*dpr;
  gcv.style.width=GW+'px';gcv.style.height=GH+'px';
  if(view==='time')buildTimeline();
}
addEventListener('resize',resize);
resize();
for(let i=0;i<260;i++)simTick();
simAlpha=.35;
/* initial camera: fit everything, centered */
{
  let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
  shownNodes().forEach(n=>{x0=Math.min(x0,n.x);y0=Math.min(y0,n.y);x1=Math.max(x1,n.x);y1=Math.max(y1,n.y);});
  const k=Math.max(.3,Math.min(1.6,.94*Math.min(GW/(x1-x0+140),GH/(y1-y0+140))));
  cam.k=k;
  cam.x=(x0+x1)/2;
  cam.y=(y0+y1)/2;
}
function frame(now){
  if(view==='graph'){simTick();drawGraph(now);}
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
renderTimelineState();
hooks.focusProject(); /* consume a List→Graph jump parked before boot */

}
