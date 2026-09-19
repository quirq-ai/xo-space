/* The workspace graph widget: modules/projects/pages/graph.json names it as
   {"type":"widget","widget":"graph","data":""}, so the page's one read
   (GET /xo/space.json) arrives whole as `data`. The drawing code is the
   canvas engine of space_ui/js/views/atlas.js (model, force layout, camera,
   render, hover, focus, expand/collapse), copied here without the parts
   that belong to the legacy atlas page: the timeline lens, the root picker,
   the todo satellites, the path reveal and the file toolbar. No fetch, poll
   or navigation of its own: the spec view feeds it and the shell owns the
   page. Exports mount(el, ctx), update(data), destroy(). */

const ACCENT='#a8d94f',ACCENT_DEEP='#83d63a';
/* Canvas text cannot read CSS variables: keep in step with --sans in base.css. */
const SANS='"Inter",system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif';
const STYLE_ID='prj-graph-style';
const CSS=`
.prj-graph{position:relative;overflow:hidden;border-radius:10px;background:#0b0c0f;color:#e9e4d9;font-family:${SANS};isolation:isolate;min-height:420px}
.prj-graph canvas{display:block;position:absolute;inset:0;touch-action:none}
.prj-graph .pg-meta{position:absolute;top:12px;left:16px;right:16px;font:400 11px ${SANS};letter-spacing:.04em;color:#a6a094;pointer-events:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.prj-graph .pg-crumb{position:absolute;top:32px;left:16px;display:none;gap:8px;align-items:center;font:400 12px ${SANS};color:#e9e4d9;background:rgba(11,12,15,.9);border:1px solid rgba(233,228,217,.12);border-radius:8px;padding:6px 10px;max-width:calc(100% - 32px)}
.prj-graph .pg-crumb.is-on{display:flex}
.prj-graph .pg-crumb b{font-weight:600}
.prj-graph .pg-crumb span{color:#a6a094}
.prj-graph .pg-crumb button,.prj-graph .pg-panel button,.prj-graph .pg-empty button{font:500 11px ${SANS};color:#e9e4d9;background:rgba(233,228,217,.08);border:1px solid rgba(233,228,217,.16);border-radius:6px;padding:4px 9px;cursor:pointer}
.prj-graph .pg-crumb button:hover,.prj-graph .pg-panel button:hover,.prj-graph .pg-empty button:hover{background:rgba(233,228,217,.16)}
.prj-graph .pg-legend{position:absolute;bottom:14px;left:16px;right:120px;display:flex;gap:6px 14px;align-items:center;flex-wrap:wrap;max-height:74px;overflow:hidden;pointer-events:none}
.prj-graph .pg-legend .li{display:flex;gap:6px;align-items:center;font:400 11px ${SANS};letter-spacing:.02em;color:#a6a094}
.prj-graph .pg-legend .sw{width:8px;height:8px;border-radius:50%}
.prj-graph .pg-legend .sw-ring{box-sizing:border-box;background:transparent;border:1px solid #a6a094}
.prj-graph .pg-legend .li-dim{opacity:.72}
.prj-graph .pg-sim{position:absolute;bottom:14px;right:16px;display:flex;gap:7px;align-items:center;font:400 11px ${SANS};letter-spacing:.06em;text-transform:uppercase;color:#a6a094;transition:opacity .4s;pointer-events:none}
.prj-graph .pg-sim .pip{width:6px;height:6px;border-radius:50%;background:${ACCENT};animation:pg-pulse 1.1s infinite}
@keyframes pg-pulse{0%,100%{opacity:.35}50%{opacity:1}}
.prj-graph .pg-hc{position:fixed;z-index:30;display:none;width:280px;background:#15171c;border:1px solid rgba(233,228,217,.14);border-radius:10px;box-shadow:0 14px 40px rgba(0,0,0,.5);pointer-events:none;overflow:hidden}
.prj-graph .pg-hc.is-on{display:block}
.prj-graph .pg-hc .art{padding:14px 16px 11px}
.prj-graph .pg-hc .kicker,.prj-graph .pg-panel .kicker{font:400 11px ${SANS};letter-spacing:.06em;text-transform:uppercase;color:#cfc9bb}
.prj-graph .pg-hc h5{font:600 16px/1.22 ${SANS};margin:5px 0 0;color:#e9e4d9}
.prj-graph .pg-hc .sub,.prj-graph .pg-panel .sub{font:400 12px ${SANS};color:#cfc9bb;margin-top:4px}
.prj-graph .pg-hc dl{display:grid;grid-template-columns:52px 1fr;gap:7px 10px;padding:10px 16px;margin:0}
.prj-graph .pg-hc dt{font:400 11px/1.6 ${SANS};letter-spacing:.06em;text-transform:uppercase;color:#a6a094}
.prj-graph .pg-hc dd{font-size:12px;line-height:1.45;color:#cfc9bb;margin:0}
.prj-graph .pg-hc dd.path,.prj-graph .pg-panel .path{font:400 12px/1.6 ${SANS};word-break:break-all;color:#a6a094}
.prj-graph .pg-hc .foot{padding:8px 16px 11px;font:400 11px ${SANS};color:#a6a094;border-top:1px solid rgba(233,228,217,.08)}
.prj-graph .pg-panel{position:absolute;top:0;right:0;bottom:0;width:min(352px,100%);background:#111317;border-left:1px solid rgba(233,228,217,.1);transform:translateX(102%);transition:transform .28s cubic-bezier(.2,.7,.2,1);display:flex;flex-direction:column;z-index:5}
.prj-graph .pg-panel.is-open{transform:translateX(0)}
.prj-graph .pg-panel .pg-close{position:absolute;top:10px;right:10px;width:28px;height:28px;padding:0;border-radius:50%;display:grid;place-items:center;font-size:16px;line-height:1}
.prj-graph .pg-panel .scroll{overflow-y:auto;flex:1}
.prj-graph .pg-panel .poster{padding:34px 18px 16px}
.prj-graph .pg-panel h3{font:600 20px/1.2 ${SANS};margin:6px 0 0;color:#e9e4d9}
.prj-graph .pg-panel .psec{padding:12px 18px;border-top:1px solid rgba(233,228,217,.08)}
.prj-graph .pg-panel h4{font:600 11px ${SANS};letter-spacing:.06em;text-transform:uppercase;color:#a6a094;margin:0 0 8px}
.prj-graph .pg-panel p{font:400 13px/1.5 ${SANS};color:#cfc9bb;margin:0}
.prj-graph .pg-panel .conn{display:grid;grid-template-columns:8px 1fr auto auto;gap:9px;align-items:center;width:100%;text-align:left;padding:6px 8px;margin:0 0 2px;background:transparent;border:0;border-radius:6px;color:#e9e4d9;font:400 12px ${SANS}}
.prj-graph .pg-panel .conn:hover{background:rgba(233,228,217,.07)}
.prj-graph .pg-panel .cdot{width:8px;height:8px;border-radius:50%}
.prj-graph .pg-panel .rel,.prj-graph .pg-panel .yr{color:#a6a094;font-size:11px}
.prj-graph .pg-panel .pacts{display:flex;gap:8px;padding:12px 18px 18px}
.prj-graph .pg-empty{position:absolute;inset:0;display:grid;place-items:center;text-align:center;padding:32px}
.prj-graph .pg-empty .eyebrow{font:600 11px ${SANS};letter-spacing:.06em;text-transform:uppercase;color:#a6a094}
.prj-graph .pg-empty p{max-width:44ch;color:#cfc9bb;font:400 14px/1.5 ${SANS};margin:10px 0 0}
`;

const esc=s=>String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const hexA=(h,a)=>{
  const hex=/^#[0-9a-f]{6}$/i.test(h)?h:'#888888';
  return `rgba(${parseInt(hex.slice(1,3),16)},${parseInt(hex.slice(3,5),16)},${parseInt(hex.slice(5,7),16)},${a})`;
};
const fmtDate=d=>{const t=new Date(d+'T00:00:00');return isFinite(t)?t.toLocaleDateString('en-US',{year:'numeric',month:'long',day:'numeric'}):String(d);};
const fmtMY=t=>new Date(t).toLocaleDateString('en-US',{year:'numeric',month:'short'});

function ensureStyle(){
  if(document.getElementById(STYLE_ID))return;
  const style=document.createElement('style');
  style.id=STYLE_ID;style.textContent=CSS;
  document.head.appendChild(style);
}

/* What a graph payload must carry before the engine will read it. */
function usable(data){
  return !!(data&&typeof data==='object'&&data.root&&typeof data.root.id==='string'
    &&Array.isArray(data.hubs)&&Array.isArray(data.groups)&&Array.isArray(data.leaves)
    &&data.categories&&typeof data.categories==='object');
}
function signature(data){
  return JSON.stringify([data.meta?.mappedOn||null,data.hubs.length,data.groups.length,data.leaves.length,(data.ties||[]).length,data.root.id]);
}

/* One widget instance per mount: the DOM it made and the engine it booted. */
let current=null;

export async function mount(el,ctx){
  ensureStyle();
  destroy();
  el.innerHTML='';
  const wrap=document.createElement('div');
  wrap.className='prj-graph';
  wrap.innerHTML=
    '<canvas></canvas>'
    +'<div class="pg-meta"></div>'
    +'<div class="pg-crumb"><b class="pg-crumb-name"></b><span class="pg-crumb-depth"></span><button type="button" class="pg-crumb-clear">Clear</button></div>'
    +'<div class="pg-legend"></div>'
    +'<div class="pg-sim" style="opacity:0"><span class="pip"></span>settling</div>'
    +'<div class="pg-hc"></div>'
    +'<aside class="pg-panel"><button type="button" class="pg-close" aria-label="Close">&times;</button><div class="scroll"></div></aside>';
  el.appendChild(wrap);
  current={el,wrap,ctx:ctx||{},engine:null,sig:null,onResize:null};
  const size=()=>{
    const top=wrap.getBoundingClientRect().top;
    wrap.style.height=Math.max(420,Math.floor(innerHeight-top-24))+'px';
    current?.engine?.resize();
  };
  current.onResize=size;
  addEventListener('resize',size);
  size();
  if(ctx&&ctx.data!==undefined)update(ctx.data);
}

export function update(data){
  if(!current)return;
  const {wrap}=current;
  if(!usable(data)){
    current.engine?.dispose();current.engine=null;current.sig=null;
    renderEmpty(wrap,data);
    return;
  }
  const sig=signature(data);
  if(current.engine&&current.sig===sig)return; /* the same map again: keep the camera and the selection */
  current.engine?.dispose();current.engine=null;
  wrap.querySelector('.pg-empty')?.remove();
  try{
    current.engine=boot(wrap,data,current.ctx);
    current.sig=sig;
    current.onResize?.();
  }catch(err){
    console.error('graph widget: could not draw the workspace graph:',err);
    current.engine=null;current.sig=null;
    renderEmpty(wrap,data,err);
  }
}

export function destroy(){
  if(!current)return;
  current.engine?.dispose();
  if(current.onResize)removeEventListener('resize',current.onResize);
  current.wrap.remove();
  current=null;
}

function renderEmpty(wrap,data,err){
  wrap.querySelector('.pg-empty')?.remove();
  const box=document.createElement('div');
  box.className='pg-empty';
  const why=err?esc(err.message||err):data&&typeof data==='object'&&Object.keys(data).length?'The payload is not a graph.':'The graph has not been built yet.';
  box.innerHTML='<div><div class="eyebrow">No graph to draw</div><p>'+why+' The map is read from <b>/xo/space.json</b>; the watcher rebuilds it from the projects root.</p></div>';
  wrap.appendChild(box);
}

/* ============================== THE ENGINE ============================== */
function boot(wrap,DATA,ctx){
  const lifetime=new AbortController(),timers=new Set(),frames=new Set();
  let disposed=false;
  function listen(target,type,listener,options={}){
    target.addEventListener(type,listener,{...(typeof options==='boolean'?{capture:options}:options),signal:lifetime.signal});
  }
  function setTimeout(callback,delay){
    const id=window.setTimeout(()=>{timers.delete(id);if(!disposed)callback();},delay);
    timers.add(id);return id;
  }
  function clearTimeout(id){timers.delete(id);window.clearTimeout(id);}
  function requestAnimationFrame(callback){
    const id=window.requestAnimationFrame(time=>{frames.delete(id);if(!disposed)callback(time);});
    frames.add(id);return id;
  }
  const dispose=()=>{
    disposed=true;lifetime.abort();
    for(const id of timers)window.clearTimeout(id);
    for(const id of frames)window.cancelAnimationFrame(id);
    timers.clear();frames.clear();
    hc.classList.remove('is-on');panel.classList.remove('is-open');crumb.classList.remove('is-on');
  };

  const gcv=wrap.querySelector('canvas'),gc=gcv.getContext('2d');
  const meta=wrap.querySelector('.pg-meta'),legend=wrap.querySelector('.pg-legend'),simstat=wrap.querySelector('.pg-sim');
  const hc=wrap.querySelector('.pg-hc'),panel=wrap.querySelector('.pg-panel'),panelScroll=panel.querySelector('.scroll');
  const crumb=wrap.querySelector('.pg-crumb'),crumbName=crumb.querySelector('.pg-crumb-name'),crumbDepth=crumb.querySelector('.pg-crumb-depth');

  /* ---- model from the payload ---- */
  const CAT=DATA.categories;
  const catOf=n=>CAT[n.cat]||{color:'#888888',name:String(n.cat||'')};
  const hubLabel=DATA.meta?.hubLabel||'Department';
  const NODES=[];
  NODES.push({id:DATA.root.id,type:'root',label:DATA.root.label||DATA.root.id,blurb:DATA.root.blurb});
  DATA.hubs.forEach(h=>NODES.push({id:h.id,type:'hub',cat:h.cat,label:h.label,blurb:h.blurb}));
  DATA.groups.forEach(g=>NODES.push({id:g.id,type:'group',cat:g.cat,label:g.label,blurb:g.blurb}));
  DATA.leaves.forEach(l=>NODES.push({
    id:l.id,type:'leaf',group:l.group,shape:l.shape,tag:l.tag,label:l.label,
    date:l.date,blurb:l.blurb,path:l.path,clusters:l.clusters||[],xotype:l.xotype
  }));
  const byId=new Map(NODES.map(n=>[n.id,n]));
  const EDGES=[];
  DATA.hubs.forEach(h=>EDGES.push({s:DATA.root.id,t:h.id,kind:'root',label:DATA.meta?.rootEdgeLabel||'a department of XO'}));
  DATA.groups.forEach(g=>{if(byId.has(g.cat))EDGES.push({s:g.cat,t:g.id,kind:'hg',label:'part of'});});
  DATA.leaves.forEach(l=>{if(byId.has(l.group))EDGES.push({s:l.group,t:l.id,kind:'rg',label:'part of'});});
  (DATA.ties||[]).forEach(x=>{if(byId.has(x.s)&&byId.has(x.t))EDGES.push({s:x.s,t:x.t,kind:'x',label:x.label});});
  NODES.forEach(n=>{
    if(n.type==='leaf')n.cat=byId.get(n.group)?.cat;
    n.adj=[];n.x=0;n.y=0;n.vx=0;n.vy=0;n.fx=null;n.fy=null;
  });
  EDGES.forEach(e=>{byId.get(e.s).adj.push({e,other:e.t});byId.get(e.t).adj.push({e,other:e.s});});
  NODES.forEach(n=>n.degree=n.adj.length);
  const LEAVES=NODES.filter(n=>n.type==='leaf');
  const GROUPS=NODES.filter(n=>n.type==='group');
  const HUBS=NODES.filter(n=>n.type==='hub');
  const noun=DATA.meta?.noun||'artifacts';
  const collectionLabel=DATA.meta?.collectionLabel||'clusters';
  meta.textContent=`${LEAVES.length} ${noun} · ${GROUPS.length} ${collectionLabel} · ${EDGES.length} links`+(DATA.meta?.mappedOn?` · mapped ${DATA.meta.mappedOn}`:'');

  const colorOf=n=>n.type==='root'?'#e9e4d9':catOf(n).color;
  function radiusOf(n){
    if(n.type==='root')return 17;
    if(n.type==='hub')return 13;
    if(n.type==='group')return 5.5+Math.min(5,n.adj.length*.22);
    return 3.3+Math.min(4.2,(n.degree-1)*.85);
  }
  NODES.forEach(n=>n.r=radiusOf(n));
  const REDUCED=matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* expansion + filter state: groups start collapsed once the workspace is
     large enough that simulating every leaf would cost more than it shows */
  const COLLAPSE_LEAVES_ABOVE=600;
  const START_COLLAPSED=LEAVES.length>COLLAPSE_LEAVES_ABOVE;
  const expanded=new Map(GROUPS.map(g=>[g.id,!START_COLLAPSED]));
  const belongsToCategory=(n,cat)=>n.cat===cat||(n.clusters||[]).includes(cat);
  const isShown=n=>n.type!=='leaf'||!!expanded.get(n.group);
  let _expVersion=0,_shownKey=null,_shownNodes=[],_shownEdges=[];
  function _refreshShown(){
    const key=String(_expVersion);
    if(key===_shownKey)return;
    _shownKey=key;
    _shownNodes=NODES.filter(isShown);
    _shownEdges=EDGES.filter(e=>isShown(byId.get(e.s))&&isShown(byId.get(e.t)));
  }
  const shownNodes=()=>{_refreshShown();return _shownNodes;};
  const shownEdges=()=>{_refreshShown();return _shownEdges;};
  const leafCountByGroup=new Map(GROUPS.map(g=>[g.id,0]));
  const leafCountByCat=new Map();
  for(const l of LEAVES){
    leafCountByGroup.set(l.group,(leafCountByGroup.get(l.group)||0)+1);
    for(const c of new Set([l.cat,...(l.clusters||[])]))leafCountByCat.set(c,(leafCountByCat.get(c)||0)+1);
  }

  /* ---- layout seed ---- */
  const HUB_ANGLE=DATA.hubAngles||{};
  HUBS.forEach((h,i)=>{if(typeof HUB_ANGLE[h.cat]!=='number')HUB_ANGLE[h.cat]=i/Math.max(1,HUBS.length)*Math.PI*2;});
  const HUB_R=520;
  const root=byId.get(DATA.root.id);root.fx=0;root.fy=0;
  HUBS.forEach(h=>{h.ax=Math.cos(HUB_ANGLE[h.cat])*HUB_R;h.ay=Math.sin(HUB_ANGLE[h.cat])*HUB_R;h.x=h.ax;h.y=h.ay;});
  const SECTOR=Math.PI*2/Math.max(1,Object.keys(HUB_ANGLE).length);
  GROUPS.forEach(g=>{
    const sib=GROUPS.filter(x=>x.cat===g.cat),k=sib.indexOf(g),m=sib.length;
    const step=Math.min(.5,SECTOR*.85/Math.max(1,m));
    const a=(HUB_ANGLE[g.cat]||0)+(k-(m-1)/2)*step;
    const r=HUB_R+170+(k%3)*70;
    g.x=Math.cos(a)*r;g.y=Math.sin(a)*r;
  });
  LEAVES.forEach((l,i)=>{
    const g=byId.get(l.group)||root;
    const a=(i*.618033*Math.PI*2)%(Math.PI*2);
    l.x=g.x+Math.cos(a)*(30+(i%5)*11);
    l.y=g.y+Math.sin(a)*(30+(i%5)*11);
  });

  /* ---- simulation ---- */
  let simAlpha=1;
  const rootId=DATA.root.id;
  const SPR={root:{d:HUB_R,k:.02},hg:{d:175,k:.05},rg:{d:62,k:.08},x:DATA.meta?.tieSpring||{d:210,k:.005}};
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
      if(n.type==='hub'){n.vx+=(n.ax-n.x)*.05*simAlpha;n.vy+=(n.ay-n.y)*.05*simAlpha;}
      else if(n.fx==null){n.vx-=(n.x-R0.x)*.001*simAlpha;n.vy-=(n.y-R0.y)*.001*simAlpha;}
      if(n.fx!=null){n.x=n.fx;n.y=n.fy;n.vx=0;n.vy=0;continue;}
      n.vx*=.7;n.vy*=.7;
      const _sp=Math.hypot(n.vx,n.vy);
      if(_sp>60){n.vx*=60/_sp;n.vy*=60/_sp;}
      n.x+=n.vx;n.y+=n.vy;
    }
    if(simAlpha>.003)simAlpha*=.9885;else simAlpha=0;
  }
  const reheat=a=>{simAlpha=Math.max(simAlpha,a);};

  /* ---- camera ---- */
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

  /* ---- render ---- */
  let GW=0,GH=0,dpr=1;
  let hoverId=null,selId=null,focusSet=null,focusDepth=0;
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
      const points=[[group.x,group.y]];
      for(const leaf of LEAVES){
        if(isShown(leaf)&&belongsToCategory(leaf,group.cat))points.push([leaf.x,leaf.y]);
      }
      if(points.length<2)continue;
      const col=catOf(group).color;
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
          gc.quadraticCurveTo(point[0],point[1],(point[0]+next[0])/2,(point[1]+next[1])/2);
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
  function halo(s,x,y,fill,tracking){
    if(tracking){gc.save();gc.letterSpacing=(tracking*10)+'px';}
    gc.lineWidth=3.5;gc.strokeStyle='rgba(11,12,15,.88)';gc.lineJoin='round';
    gc.strokeText(s,x,y);gc.fillStyle=fill;gc.fillText(s,x,y);
    if(tracking)gc.restore();
  }
  let pulseN=null;
  function drawGraph(now){
    gc.setTransform(dpr,0,0,dpr,0,0);
    gc.clearRect(0,0,GW,GH);
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
    const inFocus=id=>!focusSet||focusSet.has(id);
    if(DATA.meta?.enclose)drawEnclosures(k);
    for(const e of es){
      const a=byId.get(e.s),b=byId.get(e.t);
      let alpha,width,color;
      if(focusSet){
        const lit=(e.s===selId||e.t===selId)&&inFocus(e.s)&&inFocus(e.t);
        const semi=inFocus(e.s)&&inFocus(e.t);
        if(lit){alpha=.42;width=1.4/k;color=ACCENT;}
        else if(semi){alpha=.14;width=.8/k;color='#cfc9bb';}
        else{alpha=.012;width=.7/k;color='#78746c';}
      }else{
        alpha=e.kind==='x'?.10:e.kind==='root'?.07:.05;
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
    for(const n of vs){
      const col=colorOf(n);
      let a=1;
      if(focusSet)a=focusSet.has(n.id)?1:.14;
      gc.globalAlpha=a;
      if(n.type==='root'){
        /* the XO mark: white X chevrons, lime O chevrons */
        gc.lineWidth=2.4/Math.sqrt(k);gc.lineJoin='miter';gc.lineCap='butt';
        const sc=.075;
        const CHEV=[
          ['#e9e4d9',[[37,166],[118,247],[31,335]]],
          ['#e9e4d9',[[245,166],[163,247],[251,335]]],
          [ACCENT_DEEP,[[328,165],[247,247],[334,334]]],
          [ACCENT_DEEP,[[381,165],[462,247],[375,334]]],
        ];
        for(const[c,pts]of CHEV){
          gc.strokeStyle=c;gc.beginPath();
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
        const hl=n.id===hoverId||n.id===selId;
        const r=n.r*(hl?1.5:1);
        drawShape(gc,n.x,n.y,r,n.shape);
        if(n.shape==='ring'){gc.strokeStyle=col;gc.lineWidth=1.5/Math.sqrt(k);gc.stroke();}
        else{gc.fillStyle=col;gc.fill();}
        if(n.id===selId){
          drawShape(gc,n.x,n.y,r+3.4/Math.sqrt(k),n.shape);
          gc.strokeStyle=hexA(ACCENT,.8);gc.lineWidth=1.4/Math.sqrt(k);gc.stroke();
        }else if(hl){
          drawShape(gc,n.x,n.y,r+3/Math.sqrt(k),n.shape);
          gc.strokeStyle='rgba(233,228,217,.9)';gc.lineWidth=1.2/Math.sqrt(k);gc.stroke();
        }
      }
      gc.globalAlpha=1;
    }
    /* labels, in screen space */
    gc.setTransform(dpr,0,0,dpr,0,0);
    gc.textAlign='center';
    for(const n of vs){
      let a=1;
      if(focusSet)a=focusSet.has(n.id)?1:0;
      if(a===0)continue;
      const sx=(n.x-cam.x)*k+GW/2,sy=(n.y-cam.y)*k+GH/2;
      if(sx<-100||sx>GW+100||sy<-50||sy>GH+50)continue;
      if(n.type==='hub'){
        gc.font='500 17px '+SANS;
        halo(n.label,sx,sy-n.r*k-12,`rgba(233,228,217,${.94*a})`);
        gc.font='500 11px '+SANS;
        const hubCount=leafCountByCat.get(n.cat)||0;
        const hubNoun=hubCount===1?noun.replace(/s$/,''):noun;
        halo(`${hubCount} ${hubNoun.toUpperCase()}`,sx,sy+n.r*k+16,`rgba(166,160,148,${a})`,.06);
      }else if(n.type==='group'){
        const on=n.id===hoverId||n.id===selId||(focusSet&&focusSet.has(n.id));
        if(!(on||k>.8))continue;
        const closed=!expanded.get(n.id);
        gc.font='500 11px '+SANS;
        const t=String(n.label).toUpperCase()+(closed?` +${leafCountByGroup.get(n.id)||0}`:'');
        halo(t,sx,sy-n.r*k-7,`rgba(166,160,148,${a})`,.06);
      }else if(n.type==='leaf'){
        const on=n.id===hoverId||n.id===selId||(focusSet&&focusSet.has(n.id));
        if(!(on||k>1.55||(k>1.05&&n.degree>=4)))continue;
        gc.font='400 11px '+SANS;
        halo(n.label,sx,sy-n.r*k-7,on?`rgba(233,228,217,${.94*a})`:`rgba(166,160,148,${.9*a})`);
      }
    }
    gc.globalAlpha=1;
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
    simstat.style.opacity=simAlpha>.05?1:0;
  }

  /* ---- interaction ---- */
  let drag=null,pan=false,downX=0,downY=0,moved=false,lastX=0,lastY=0;
  const toWorld=(mx,my)=>({x:(mx-GW/2)/cam.k+cam.x,y:(my-GH/2)/cam.k+cam.y});
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
  listen(gcv,'pointerdown',e=>{
    gcv.setPointerCapture(e.pointerId);
    downX=lastX=e.clientX;downY=lastY=e.clientY;moved=false;
    const n=pick(...evXY(e));
    if(n&&n.type!=='root'){drag=n;n.fx=n.x;n.fy=n.y;}
    else pan=true;
    camAnim=null;
  });
  listen(gcv,'pointermove',e=>{
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
      const n=pick(...evXY(e));
      hoverId=n?n.id:null;
      gcv.style.cursor=n?'pointer':'default';
      if(n)showHC(n,e.clientX,e.clientY);else hideHC();
    }
  });
  listen(gcv,'pointerleave',()=>{if(!drag&&!pan)hideHC();});
  let lastUp=0,clickT=null;
  listen(gcv,'pointerup',e=>{
    if(drag){
      const d=drag;drag=null;
      if(d.type!=='root'){d.fx=null;d.fy=null;}
    }
    pan=false;
    if(moved)return;
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
    if(!n){clearFocus();return;}
    select(n.id,1);
  }
  function onDbl(n){
    if(!n)return;
    if(n.type==='group'){toggleGroup(n);return;}
    if(n.type==='hub'){
      const gs=GROUPS.filter(g=>g.cat===n.cat);
      const anyClosed=gs.some(g=>!expanded.get(g.id));
      gs.forEach(g=>setExp(g,anyClosed));reheat(.5);
      return;
    }
    if(selId===n.id&&focusDepth===1)select(n.id,2);
    else select(n.id,2);
  }
  const PANEL_W=352;
  function select(id,depth,fly=true){
    selId=id;focusDepth=depth;
    focusSet=neighborhood(id,depth);
    const n=byId.get(id);
    crumbName.textContent=n.label;
    crumbDepth.textContent=`${depth} hop${depth>1?'s':''} · ${focusSet.size} nodes`;
    crumb.classList.add('is-on');
    openPanel(n);
    if(fly){
      const kT=Math.max(cam.k,1.6);
      const off=GW>760?PANEL_W/2/kT:0;
      flyTo(n.x+off,n.y,kT);
    }
  }
  function clearFocus(){
    selId=null;focusSet=null;focusDepth=0;
    crumb.classList.remove('is-on');
    closePanel();
  }
  function setExp(g,v){
    if(expanded.get(g.id)===v)return;
    expanded.set(g.id,v);
    _expVersion++;
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
  function ensureShown(n){
    if(n.type==='leaf'&&!expanded.get(n.group)){setExp(byId.get(n.group),true);reheat(.4);}
  }
  listen(gcv,'wheel',e=>{
    e.preventDefault();camAnim=null;
    const f=Math.exp(-e.deltaY*.0016);
    const nk=Math.max(.22,Math.min(5,cam.k*f));
    const [mx,my]=evXY(e);
    const w=toWorld(mx,my);
    cam.x=w.x-(mx-GW/2)/nk;
    cam.y=w.y-(my-GH/2)/nk;
    cam.k=nk;
  },{passive:false});
  listen(crumb.querySelector('.pg-crumb-clear'),'click',()=>clearFocus());
  listen(window,'keydown',e=>{
    if(e.defaultPrevented||!wrap.isConnected)return;
    const active=document.activeElement;
    if(/INPUT|TEXTAREA|SELECT/.test(active?.tagName||'')||active?.isContentEditable)return;
    if(e.key==='Escape'){clearFocus();hideHC();}
  });

  /* ---- legend ---- */
  {
    const glyph={
      disc:'<svg width="10" height="10"><circle cx="5" cy="5" r="3.6" fill="#b3ada0"/></svg>',
      ring:'<svg width="10" height="10"><circle cx="5" cy="5" r="3.1" fill="none" stroke="#b3ada0" stroke-width="1.4"/></svg>',
      diamond:'<svg width="10" height="10"><path d="M5 .9 9.1 5 5 9.1.9 5Z" fill="#b3ada0"/></svg>',
      stack:'<svg width="11" height="10"><rect x="1" y="3" width="6" height="6" fill="none" stroke="#b3ada0"/><rect x="4" y="1" width="6" height="6" fill="#b3ada0"/></svg>',
      slab:'<svg width="12" height="10"><rect x=".5" y="2.7" width="11" height="4.6" fill="#b3ada0"/></svg>'
    };
    const shapeDefs=DATA.meta?.shapeLegend||[{shape:'disc',label:'code'},{shape:'ring',label:'document'},{shape:'diamond',label:'experiment'}];
    const typeDefs=DATA.meta?.typeLegend||[];
    legend.innerHTML=
      Object.values(CAT).filter(c=>c&&typeof c==='object').map(c=>`<span class="li"><span class="sw" style="background:${esc(c.color)}"></span>${esc(c.name)}</span>`).join('')+
      shapeDefs.map((d,i)=>`<span class="li"${i===0?' style="margin-left:6px"':''}>${glyph[d.shape]||glyph.disc}${esc(d.label)}</span>`).join('')+
      typeDefs.map((d,i)=>`<span class="li${d.weight==='dim'?' li-dim':''}"${i===0?' style="margin-left:6px"':''}><span class="sw sw-ring"></span>${esc(String(d.label||'').toLowerCase())}</span>`).join('');
  }

  /* ---- hover card ---- */
  function showHC(n,mx,my){
    const col=n.type==='root'?ACCENT_DEEP:catOf(n).color;
    const kick=n.type==='hub'?hubLabel:n.type==='group'?'Cluster':n.type==='root'?'The center':`${catOf(n).name} · ${n.tag||''}`;
    const art=`linear-gradient(155deg, ${hexA(col,.24)}, ${hexA(col,.03)} 68%)`;
    let rows='';
    if(n.type==='leaf'){
      rows=`<dl>
        ${n.date?`<dt>Born</dt><dd>${esc(fmtDate(n.date))}</dd>`:''}
        ${n.path?`<dt>Where</dt><dd class="path">${esc(n.path)}</dd>`:''}
        <dt>Ties</dt><dd>${n.degree-1} connection${n.degree-1===1?'':'s'}${byId.get(n.group)?' · '+esc(byId.get(n.group).label):''}</dd>
      </dl>`;
    }else if(n.type==='group'){
      const kids=LEAVES.filter(l=>belongsToCategory(l,n.cat));
      const dates=kids.map(x=>x.date).filter(Boolean).sort();
      const span=dates.length?`<dt>Span</dt><dd>${fmtMY(+new Date(dates[0]))} to ${fmtMY(+new Date(dates.at(-1)))}</dd>`:'';
      rows=`<dl><dt>Holds</dt><dd>${kids.length} ${esc(noun)}</dd>${span}</dl>`;
    }else{
      const kids=n.type==='hub'?LEAVES.filter(l=>belongsToCategory(l,n.cat)):LEAVES;
      rows=`<dl><dt>Holds</dt><dd>${kids.length} ${esc(noun)}</dd></dl>`;
    }
    hc.innerHTML=`
      <div class="art" style="background:${art}">
        <div class="kicker">${esc(kick)}</div>
        <h5>${esc(n.label)}</h5>
        ${n.type==='leaf'?'':`<div class="sub">${esc((n.blurb||'').split('. ')[0])}</div>`}
      </div>
      ${rows}
      <div class="foot">${n.type==='group'?'Click to focus · Double-click to open or close':'Click to focus · Double-click to expand'}</div>`;
    hc.classList.add('is-on');
    const r=hc.getBoundingClientRect();
    let x=mx+18,y=my+18;
    if(x+r.width>innerWidth-8)x=mx-r.width-18;
    if(y+r.height>innerHeight-8)y=my-r.height-18;
    hc.style.left=Math.max(8,x)+'px';hc.style.top=Math.max(8,y)+'px';
  }
  function hideHC(){hc.classList.remove('is-on');hoverId=null;}

  /* ---- detail panel ---- */
  const previewable=n=>n.type==='leaf'&&!!n.path&&String(n.path).includes('/');
  function openPanel(n){
    const col=n.type==='root'?ACCENT_DEEP:catOf(n).color;
    const kick=n.type==='hub'?`${hubLabel} · ${LEAVES.filter(l=>belongsToCategory(l,n.cat)).length} ${noun}`
      :n.type==='group'?`${catOf(n).name} · environment`
      :n.type==='root'?'The center'
      :`${catOf(n).name} · ${n.tag||''}`;
    const conns=n.adj
      .filter(({other})=>byId.get(other).type!=='root'||n.type==='hub')
      .sort((p,q)=>(p.e.kind==='x'?0:1)-(q.e.kind==='x'?0:1))
      .slice(0,24)
      .map(({e,other})=>{
        const o=byId.get(other);
        let rel;
        if(e.kind==='x')rel=(e.s===n.id?'':'from ')+(e.label||'tie');
        else rel=o.type==='group'||o.type==='hub'||o.type==='root'?'part of':'holds';
        return `<button type="button" class="conn" data-id="${esc(o.id)}">
          <span class="cdot" style="background:${o.type==='root'?ACCENT_DEEP:esc(catOf(o).color)}"></span>
          <span>${esc(o.label)}</span>
          <span class="rel">${esc(rel)}</span>
          <span class="yr">${o.date?esc(String(o.date).slice(0,7)):''}</span>
        </button>`;
      }).join('');
    panelScroll.innerHTML=`
      <div class="poster" style="background:radial-gradient(120% 100% at 20% 0%, ${hexA(col,.20)}, transparent 62%)">
        <div class="kicker">${esc(kick)}</div>
        <h3>${esc(n.label)}</h3>
        ${n.date?`<div class="sub">${esc(fmtDate(n.date))}</div>`:''}
        ${n.path?`<div class="path">${esc(n.path)}</div>`:''}
      </div>
      ${n.blurb?`<div class="psec"><h4>About</h4><p>${esc(n.blurb)}</p></div>`:''}
      ${conns?`<div class="psec"><h4>Connections</h4>${conns}</div>`:''}
      ${previewable(n)?`<div class="pacts"><button type="button" data-act="preview">Preview file</button></div>`:''}`;
    panel.classList.add('is-open');
    panel.dataset.id=n.id;
  }
  function closePanel(){panel.classList.remove('is-open');}
  listen(panel.querySelector('.pg-close'),'click',()=>clearFocus());
  listen(panel,'click',e=>{
    const c=e.target.closest('.conn');
    if(c){
      const n=byId.get(c.dataset.id);
      if(!n)return;
      ensureShown(n);
      select(n.id,1);
      pulseN={id:n.id,t0:performance.now()};
      return;
    }
    const a=e.target.closest('[data-act]');
    if(!a)return;
    const n=byId.get(panel.dataset.id);
    if(a.dataset.act==='preview'&&n&&previewable(n)){
      /* the shell's previewer listens for this, as it does for the atlas */
      const cut=n.path.indexOf('/');
      dispatchEvent(new CustomEvent('space:preview-file',{detail:{
        project:n.path.slice(0,cut),path:n.path.slice(cut+1),name:n.label}}));
    }
  });

  /* ---- size, warm-up, frame loop ---- */
  function resize(){
    dpr=window.devicePixelRatio||1;
    GW=wrap.clientWidth;GH=wrap.clientHeight;
    gcv.width=Math.max(1,Math.round(GW*dpr));gcv.height=Math.max(1,Math.round(GH*dpr));
    gcv.style.width=GW+'px';gcv.style.height=GH+'px';
  }
  const WARM_TICKS=260;
  let warmLeft=WARM_TICKS,warmDone=false;
  function warmStep(budgetMs){
    const t0=performance.now();
    while(warmLeft>0&&performance.now()-t0<budgetMs){simTick();warmLeft--;}
    if(warmLeft===0&&!warmDone){warmDone=true;simAlpha=.35;}
    return warmDone;
  }
  function fitAll(){
    let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
    shownNodes().forEach(n=>{x0=Math.min(x0,n.x);y0=Math.min(y0,n.y);x1=Math.max(x1,n.x);y1=Math.max(y1,n.y);});
    if(x1<x0||GW<50)return;
    cam.k=Math.max(.3,Math.min(1.6,.94*Math.min(GW/(x1-x0+140),GH/(y1-y0+140))));
    cam.x=(x0+x1)/2;
    cam.y=(y0+y1)/2;
  }
  resize();
  warmStep(24);
  fitAll();
  let fitted=GW>=50;
  function frame(now){
    if(wrap.clientWidth===0||!wrap.isConnected){
      /* hidden page: no drawing, a slow heartbeat until it shows again */
      setTimeout(()=>requestAnimationFrame(frame),250);
      return;
    }
    if(gcv.width!==Math.round(wrap.clientWidth*dpr)||gcv.height!==Math.round(wrap.clientHeight*dpr))resize();
    if(!fitted){fitAll();fitted=true;}
    if(!warmDone){
      warmStep(8);
      if(!camAnim)fitAll();
    }else if(simAlpha>0){
      simTick();
    }
    drawGraph(now);
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  return{dispose,resize};
}
