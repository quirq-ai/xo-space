/* The Snapshot view: one commit as a 3D city you can fly around.

   Opened from the Timeline's By-project mode — a commit-day dot resolves
   to shas and lands here (never from the tab bar: nav:false,
   parent:'time'). Views never import each other, so the timeline
   dispatches `space:show-commit` with {project, sha}; a request that
   arrives before this module mounts parks until show() consumes it.

   This is a port of Space Walk's citymap (mindwalk web/src/scene/
   CityScene.tsx + internal/citymap/builder.go) onto a commit instead of a
   session, keeping its grammar:

   - the 120-unit plain, squarified with 0.08-unit inset streets at every
     level and aspect capped at 40 (builder.go);
   - file weight sqrt(max(bytes/4096, 16)) — its fileWeight with the byte
     fallback, since a bare git tree has no cheap line counts;
   - district plates depth-shaded #161a20 → #242832 with hairline borders,
     flat file tiles #56534b carrying the scene's FNV-1a lightness jitter;
   - map-style label LOD: a district is named while its subtree spans
     enough screen pixels, budgeted by file count, collision losers
     dropped — reprojected every time the camera moves;
   - light is data. The plain stays dark and flat; only what the commit
     touched rises and glows, in the scene's own touch lattice — added
     #a8d94f (edit), modified #a8a24e (hit), renamed #9dc0e8 (read).

   Where Space Walk earns height from attention (touch depth × revisits),
   a commit earns it from CHURN: a column's height is the lines it added
   plus deleted, log-scaled with a gamma so a 400-line rewrite towers over
   a one-line tweak instead of both reading as "touched". Binary files
   change by an uncountable amount, so they get a distinct capped marker
   rather than a fake zero. A second height mode raises every file by size
   instead, which is Space Walk's own locHeights fallback for a static map
   — useful for reading the repo's shape rather than the commit's.

   three.js is vendored (space_ui/vendor/) and dynamically imported the
   first time this view mounts: build-free like the rest of the app, and
   the ~760 KB never loads for anyone who does not open a snapshot. */
import {API_BASE,apiFetch} from '../core/api.js';

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

let THREE=null,OrbitControls=null;   /* filled by the lazy import */
let root=null,host=null,hc=null;
let go=()=>{};
let visible=false;
let pending=null;                    /* {project,sha} parked until show() */
let cur=null,snap=null,world=null;
let token=0;
let heightMode='churn';              /* 'churn' | 'size' */
let sceneKit=null;                   /* renderer/camera/controls, built once */
let cityGroup=null,labelSet=null;
let fileRecs=[],hoverId=null;

addEventListener('space:show-commit',e=>{
  pending={project:String(e.detail?.project||''),sha:String(e.detail?.sha||'')};
  if(visible)consumePending();
});

export default{
  id:'snapshot',label:'Snapshot',order:2.7,nav:false,parent:'time',
  async mount(el,ctx){
    root=el;go=ctx.switchTo;
    root.innerHTML=`
      <div class="snap-head" id="snap-head"></div>
      <div class="snap-stage" id="snap-stage"></div>
      <div class="snap-legend" id="snap-legend" hidden></div>
      <div class="snap-empty" id="snap-empty">
        <div class="eyebrow">Commit snapshot</div>
        <p>Open the <b>Timeline</b>, switch it to <b>By project</b>, and click
        a commit dot to fly over that repository as it stood at that moment.</p>
      </div>
      <div id="snap-hc"></div>`;
    host=root.querySelector('#snap-stage');
    hc=root.querySelector('#snap-hc');
  },
  show(){visible=true;if(!consumePending()&&sceneKit)sceneKit.resize();},
  hide(){visible=false;hideHC();}
};

function consumePending(){
  if(!pending)return false;
  const t=pending;pending=null;
  if(cur&&cur.project===t.project&&cur.sha===t.sha)return true;
  load(t);
  return true;
}

/* ---------------- load ---------------- */

async function load(t){
  cur=t;snap=null;world=null;hideHC();
  const mine=++token;
  headHTML(`<button class="snap-back" id="snap-back">&larr; Timeline</button>
    <span class="snap-loading">reading ${esc(t.project)} @ ${esc(t.sha.slice(0,7))}&hellip;</span>`);
  root.querySelector('#snap-empty').hidden=true;
  root.querySelector('#snap-legend').hidden=true;

  const [res]=await Promise.all([
    apiFetch(API_BASE+'/api/xo-projects/'+encodeURIComponent(t.project)
      +'/commits/'+encodeURIComponent(t.sha)+'/snapshot'),
    ensureThree(),
  ]);
  if(mine!==token)return;                 /* a newer commit owns the screen */
  if(!THREE){
    headHTML(`<button class="snap-back" id="snap-back">&larr; Timeline</button>
      <span class="snap-err">3D renderer failed to load</span>`);
    return;
  }
  if(!res.ok){
    headHTML(`<button class="snap-back" id="snap-back">&larr; Timeline</button>
      <span class="snap-err">${esc(res.offline?'xo-cowork-api is unreachable'
        :res.error||'could not read this commit')}</span>`);
    return;
  }
  snap=res.data;
  world=layoutWorld();
  renderHead();
  buildCity();
}

/* One dynamic import, cached. A failure leaves THREE null and the caller
   shows a truthful error rather than a blank stage. */
let threePromise=null;
function ensureThree(){
  if(THREE)return Promise.resolve();
  if(!threePromise)threePromise=(async()=>{
    try{
      const [three,orbit]=await Promise.all([
        import('../../vendor/three.module.min.js'),
        import('../../vendor/OrbitControls.js'),
      ]);
      THREE=three;OrbitControls=orbit.OrbitControls;
    }catch(err){console.error('Snapshot: three.js failed to load:',err);}
  })();
  return threePromise;
}

/* ---------------- header ---------------- */

function headHTML(inner){
  const el=root.querySelector('#snap-head');
  el.innerHTML=inner;
  el.querySelector('#snap-back')?.addEventListener('click',()=>go('time'));
}

function renderHead(){
  const c=snap.commit,t=snap.touched||{},d=snap.deleted||[];
  let a=0,m=0,r=0;
  for(const st of Object.values(t)){if(st==='A')a++;else if(st==='R')r++;else m++;}
  const{added,deleted}=totalChurn();
  headHTML(`
    <button class="snap-back" id="snap-back">&larr; Timeline</button>
    <div class="snap-title">
      <b>${esc(cur.project)}</b>
      <code>${esc(c.short)}</code>
      <span class="snap-subj" title="${esc(c.subject)}">${esc(c.subject)}</span>
    </div>
    <div class="snap-facts">
      <span>${esc((c.date||'').slice(0,10))}</span>
      <span>${snap.total_files.toLocaleString()} files</span>
      ${added||deleted?`<span class="tAdd">+${added.toLocaleString()}</span>
        <span class="tDel">&minus;${deleted.toLocaleString()}</span>`:''}
      ${a?`<span class="tA">${a} added</span>`:''}
      ${m?`<span class="tM">${m} modified</span>`:''}
      ${r?`<span class="tR">${r} renamed</span>`:''}
      ${d.length?`<span class="tD" title="${esc(d.slice(0,12).join('\n'))}">${d.length} deleted</span>`:''}
      ${snap.truncated?`<span class="tD">largest ${snap.tree.length} of ${snap.total_files}</span>`:''}
    </div>
    <div class="atlas-lens-switch snap-mode" id="snap-mode" aria-label="Terrain height">
      <button type="button" data-h="churn"${heightMode==='churn'?' class="is-on"':''}>Change</button>
      <button type="button" data-h="size"${heightMode==='size'?' class="is-on"':''}>Size</button>
    </div>`);
  root.querySelector('#snap-mode').addEventListener('click',e=>{
    const b=e.target.closest('[data-h]');
    if(!b||b.dataset.h===heightMode)return;
    heightMode=b.dataset.h;
    root.querySelectorAll('#snap-mode [data-h]').forEach(x=>
      x.classList.toggle('is-on',x.dataset.h===heightMode));
    renderLegend();
    buildCity();  /* geometry changes, so the city is rebuilt in place */
  });
  renderLegend();
}

function totalChurn(){
  let added=0,deleted=0;
  for(const c of Object.values(snap.churn||{})){
    added+=c.added||0;deleted+=c.deleted||0;
  }
  return{added,deleted};
}

function renderLegend(){
  const el=root.querySelector('#snap-legend');
  el.hidden=false;
  el.innerHTML=heightMode==='churn'
    ? `<span class="lg-h">Height = lines changed</span>
       <span><i style="background:${TOUCH.A}"></i>added</span>
       <span><i style="background:${TOUCH.M}"></i>modified</span>
       <span><i style="background:${TOUCH.R}"></i>renamed</span>
       <span><i class="lg-bin"></i>binary</span>
       <span class="lg-note">drag to orbit · scroll to zoom · click a file to read it at this commit</span>`
    : `<span class="lg-h">Height = file size</span>
       <span><i style="background:${LOC_RAMP[0][1]}"></i>small</span>
       <span><i style="background:${LOC_RAMP[LOC_RAMP.length-1][1]}"></i>large</span>
       <span class="lg-note">the repository's shape at this commit; touched files stay lit</span>`;
}

/* ================= layout — citymap's builder, ported ================= */

const WORLD=120,INSET=0.08,ASPECT_CAP=40,MIN_TILE=0.45;
const weightOf=bytes=>Math.sqrt(Math.max((bytes||0)/4096,16));

function buildTree(){
  const rootNode={name:'',path:'',depth:0,dirs:new Map(),files:[],w:0};
  for(const e of snap.tree||[]){
    const parts=e.path.split('/');
    let d=rootNode;
    for(let i=0;i<parts.length-1;i++){
      let next=d.dirs.get(parts[i]);
      if(!next){
        next={name:parts[i],path:d.path?d.path+'/'+parts[i]:parts[i],
          depth:d.depth+1,dirs:new Map(),files:[],w:0};
        d.dirs.set(parts[i],next);
      }
      d=next;
    }
    d.files.push({name:parts[parts.length-1],path:e.path,size:e.size,w:weightOf(e.size)});
  }
  (function sum(d){
    d.w=d.files.reduce((s,f)=>s+f.w,0);
    d.fileCount=d.files.length;
    for(const k of d.dirs.values()){d.w+=sum(k);d.fileCount+=k.fileCount;}
    if(d.w<=0)d.w=1;
    return d.w;
  })(rootNode);
  return rootNode;
}
const inset=(r,pad)=>{
  const o={...r};
  if(o.w>pad*2){o.x+=pad;o.w-=pad*2;}
  if(o.h>pad*2){o.y+=pad;o.h-=pad*2;}
  return o;
};
const capAspect=r=>{
  const o={...r};
  if(o.w<=0||o.h<=0)return o;
  if(o.w/o.h>ASPECT_CAP){const nw=o.h*ASPECT_CAP;o.x+=(o.w-nw)/2;o.w=nw;}
  else if(o.h/o.w>ASPECT_CAP){const nh=o.w*ASPECT_CAP;o.y+=(o.h-nh)/2;o.h=nh;}
  return o;
};
function squarify(items,x,y,w,h,out){
  items=items.filter(i=>i.w>0);
  if(!items.length||w<=0||h<=0)return;
  const total=items.reduce((s,i)=>s+i.w,0);
  const scale=w*h/total;
  let row=[],rowW=0,i=0;
  const worst=(sum,min,max,side)=>{
    const s2=sum*sum,side2=side*side;
    return Math.max(side2*max/s2,s2/(side2*min));
  };
  while(i<items.length){
    const it=items[i],aw=it.w*scale;
    const side=Math.min(w,h);
    if(row.length){
      const min=Math.min(...row.map(r=>r.a)),max=Math.max(...row.map(r=>r.a));
      if(worst(rowW+aw,Math.min(min,aw),Math.max(max,aw),side)>worst(rowW,min,max,side)){
        ({x,y,w,h}=flushRow(row,rowW,x,y,w,h,out));
        row=[];rowW=0;continue;
      }
    }
    row.push({it,a:aw});rowW+=aw;i++;
  }
  if(row.length)flushRow(row,rowW,x,y,w,h,out);
}
function flushRow(row,rowW,x,y,w,h,out){
  if(w>=h){
    const sw=rowW/h;let cy=y;
    for(const r of row){const rh=r.a/sw;out.push({it:r.it,x,y:cy,w:sw,h:rh});cy+=rh;}
    return{x:x+sw,y,w:w-sw,h};
  }
  const sh=rowW/w;let cx=x;
  for(const r of row){const rw=r.a/sh;out.push({it:r.it,x:cx,y,w:rw,h:sh});cx+=rw;}
  return{x,y:y+sh,w,h:h-sh};
}
function layoutWorld(){
  const tree=buildTree();
  const dirs=[],files=[];
  (function layoutNode(n,rect){
    if(n.path)dirs.push({path:n.path,name:n.name,depth:n.depth,fileCount:n.fileCount,rect});
    const kids=[
      ...[...n.dirs.values()].map(d=>({kind:'dir',node:d,w:d.w})),
      ...n.files.map(f=>({kind:'file',file:f,w:f.w})),
    ].sort((a,b)=>b.w-a.w);
    const cells=[];
    squarify(kids,rect.x,rect.y,rect.w,rect.h,cells);
    for(const c of cells){
      const r=capAspect(inset(c,INSET));
      if(c.it.kind==='dir')layoutNode(c.it.node,r);
      else files.push({...c.it.file,rect:r});
    }
  })(tree,{x:0,y:0,w:WORLD,h:WORLD});
  return{dirs,files};
}

/* ================= the palette, from web/src/scene ================= */

const SKY='#0b0c0f',GROUND='#101318';
const EDGE_BASE='#242832';
const TOUCH={A:'#a8d94f',M:'#a8a24e',R:'#9dc0e8'};  /* edit / hit / read */
const SELECTED='#e9e4d9';
const BINARY='#c8674c';        /* EMBER: churn exists but is uncountable */
const TILE_H=0.14;
const LABEL_Y=2.4;
const LABEL_MIN_SUBTREE_PX=60,LABEL_BUDGET=120;
/* Space Walk's canonical god-view is [0.46,1.08,0.72]. A commit map wants
   to be read, not flown through, so the opening framing sits steeper —
   closer to the near-overhead angle the reference citymap is shown at,
   where districts read as regions and column heights still separate.
   OrbitControls hands the angle straight back to the reader. */
const VIEW_DIR=[0.38,1.46,0.58];

/* Space Walk's locHeights ramp, for the by-size mode */
const LOC_MIN_H=0.3,LOC_MAX_H=16,LOC_HEIGHT_GAMMA=2.2;
const LOC_RAMP=[[0,'#233d53'],[0.35,'#336288'],[0.7,'#4b89bb'],[1,'#6cb2ec']];

/* Churn terrain. Space Walk earns height from attention; a commit earns it
   from lines changed. log2 keeps a 4000-line vendored diff from dwarfing
   everything, gamma>1 keeps small edits low so the big ones read as towers,
   and the floor guarantees every touched file is visibly off the plain. */
const CHURN_MIN_H=0.8,CHURN_MAX_H=22,CHURN_GAMMA=1.55;
const CHURN_FULL=2000;   /* lines at which a column reaches full height */
function churnHeight(lines){
  const t=Math.min(1,Math.log2(Math.max(lines,1)+1)/Math.log2(CHURN_FULL));
  return CHURN_MIN_H+Math.pow(t,CHURN_GAMMA)*(CHURN_MAX_H-CHURN_MIN_H);
}
function locFraction(bytes,maxLog){
  if(maxLog<=0)return 0;
  return Math.min(1,Math.log2(Math.max(bytes,1))/maxLog);
}
function rampColor(t){
  for(let i=1;i<LOC_RAMP.length;i++){
    if(t<=LOC_RAMP[i][0]){
      const [a0,c0]=LOC_RAMP[i-1],[a1,c1]=LOC_RAMP[i];
      const span=a1-a0,k=span>0?(t-a0)/span:0;
      return new THREE.Color(c0).lerp(new THREE.Color(c1),k);
    }
  }
  return new THREE.Color(LOC_RAMP[LOC_RAMP.length-1][1]);
}
function plateShade(depth){
  return new THREE.Color('#161a20').lerp(new THREE.Color('#242832'),Math.min(depth,3)/3);
}
function tileColor(path){
  let h=2166136261;
  for(let i=0;i<path.length;i++)h=Math.imul(h^path.charCodeAt(i),16777619);
  const jitter=((h>>>0)%1000)/1000-0.5;
  return new THREE.Color('#56534b').offsetHSL(0,0,jitter*0.05);
}
/* churn for one path: total lines, and whether git could count them */
function churnOf(path){
  const c=(snap.churn||{})[path];
  if(!c)return{lines:0,binary:false};
  if(c.added==null&&c.deleted==null)return{lines:0,binary:true};
  return{lines:(c.added||0)+(c.deleted||0),binary:false};
}

/* ================= the scene ================= */

function ensureScene(){
  if(sceneKit)return sceneKit;
  const scene=new THREE.Scene();
  scene.background=new THREE.Color(SKY);
  const camera=new THREE.PerspectiveCamera(38,1,0.1,2400);
  const renderer=new THREE.WebGLRenderer({antialias:true});
  renderer.setPixelRatio(Math.min(devicePixelRatio||1,2));
  host.appendChild(renderer.domElement);
  const controls=new OrbitControls(camera,renderer.domElement);
  controls.enableDamping=true;controls.dampingFactor=0.08;
  controls.maxPolarAngle=Math.PI/2.05;   /* never drop under the plain */

  scene.add(new THREE.HemisphereLight('#6f93ad','#101318',1.7));
  const moon=new THREE.DirectionalLight('#9db8cc',1.1);
  moon.position.set(60,120,40);
  scene.add(moon);

  const raycaster=new THREE.Raycaster();
  const pointer=new THREE.Vector2();
  let raf=null;

  function resize(){
    const w=host.clientWidth,h=host.clientHeight;
    if(!w||!h)return;
    camera.aspect=w/h;camera.updateProjectionMatrix();
    renderer.setSize(w,h,false);
  }
  const ro=new ResizeObserver(resize);ro.observe(host);

  function frame(){
    raf=requestAnimationFrame(frame);
    controls.update();
    if(labelSet)labelSet.update(camera,host.clientWidth,host.clientHeight);
    renderer.render(scene,camera);
  }
  raf=requestAnimationFrame(frame);

  /* pick: nearest terrain column or tile under the pointer */
  function pick(ev){
    const r=renderer.domElement.getBoundingClientRect();
    pointer.set(((ev.clientX-r.left)/r.width)*2-1,-((ev.clientY-r.top)/r.height)*2+1);
    raycaster.setFromCamera(pointer,camera);
    const targets=[];
    if(cityGroup?.userData.tiles)targets.push(cityGroup.userData.tiles);
    if(cityGroup?.userData.terrain)targets.push(cityGroup.userData.terrain);
    const hit=raycaster.intersectObjects(targets,false)[0];
    if(!hit||hit.instanceId==null)return null;
    const map=hit.object===cityGroup.userData.terrain
      ? cityGroup.userData.terrainIds : cityGroup.userData.tileIds;
    return fileRecs[map[hit.instanceId]]||null;
  }
  renderer.domElement.addEventListener('pointermove',ev=>{
    const rec=pick(ev);
    setHover(rec,ev);
  });
  renderer.domElement.addEventListener('pointerleave',()=>setHover(null));
  renderer.domElement.addEventListener('click',ev=>{
    const rec=pick(ev);
    if(!rec)return;
    dispatchEvent(new CustomEvent('space:preview-file',{detail:{
      project:cur.project,path:rec.path,name:rec.name,ref:cur.sha
    }}));
  });

  sceneKit={scene,camera,renderer,controls,resize,
    dispose(){cancelAnimationFrame(raf);ro.disconnect();}};
  resize();
  return sceneKit;
}

function disposeCity(){
  if(!cityGroup)return;
  cityGroup.traverse(o=>{
    if(o.geometry)o.geometry.dispose();
    if(o.material){
      const mats=Array.isArray(o.material)?o.material:[o.material];
      for(const m of mats){if(m.map&&!m.map.userData?.shared)m.map.dispose();m.dispose();}
    }
  });
  sceneKit.scene.remove(cityGroup);
  cityGroup=null;labelSet=null;
}

function buildCity(){
  const kit=ensureScene();
  disposeCity();
  root.querySelector('#snap-empty').hidden=true;
  if(!world||!world.files.length)return;

  const group=new THREE.Group();
  const cx=WORLD/2,cz=WORLD/2;         /* recentre the plain on the origin */
  const touched=snap.touched||{};

  kit.scene.fog=new THREE.Fog(new THREE.Color(SKY),WORLD*2.1,WORLD*4.2);

  /* ground + grid */
  const ground=new THREE.Mesh(
    new THREE.PlaneGeometry(WORLD*6,WORLD*6),
    new THREE.MeshStandardMaterial({color:GROUND,roughness:1}));
  ground.rotation.x=-Math.PI/2;ground.position.y=-0.32;
  group.add(ground);
  const grid=new THREE.GridHelper(WORLD*2.8,46,EDGE_BASE,'#1b1f27');
  grid.material.transparent=true;grid.material.opacity=0.5;grid.position.y=-0.3;
  group.add(grid);

  /* district plates, depth-shaded, plus hairline borders */
  const plateDirs=world.dirs.filter(d=>d.depth<=3&&d.rect.w>0&&d.rect.h>0);
  if(plateDirs.length){
    const plates=new THREE.InstancedMesh(
      new THREE.BoxGeometry(1,1,1),
      new THREE.MeshStandardMaterial({roughness:0.95,metalness:0}),
      plateDirs.length);
    const mtx=new THREE.Matrix4();
    plateDirs.forEach((d,i)=>{
      const top=-0.2+Math.min(d.depth,3)*0.06,height=top+0.3;
      mtx.compose(
        new THREE.Vector3(d.rect.x+d.rect.w/2-cx,top-height/2,d.rect.y+d.rect.h/2-cz),
        new THREE.Quaternion(),
        new THREE.Vector3(d.rect.w,height,d.rect.h));
      plates.setMatrixAt(i,mtx);
      plates.setColorAt(i,plateShade(d.depth));
    });
    plates.instanceMatrix.needsUpdate=true;
    if(plates.instanceColor)plates.instanceColor.needsUpdate=true;
    group.add(plates);

    const pos=[];
    for(const d of plateDirs){
      if(d.depth<1)continue;
      const y=-0.2+Math.min(d.depth,3)*0.06+0.02;
      const x0=d.rect.x-cx,x1=d.rect.x+d.rect.w-cx;
      const z0=d.rect.y-cz,z1=d.rect.y+d.rect.h-cz;
      pos.push(x0,y,z0,x1,y,z0, x1,y,z0,x1,y,z1, x1,y,z1,x0,y,z1, x0,y,z1,x0,y,z0);
    }
    if(pos.length){
      const g=new THREE.BufferGeometry();
      g.setAttribute('position',new THREE.Float32BufferAttribute(pos,3));
      group.add(new THREE.LineSegments(g,
        new THREE.LineBasicMaterial({color:EDGE_BASE,transparent:true,opacity:0.9})));
    }
  }

  /* every file exists on the map as a flat dark tile — the fog of war */
  fileRecs=world.files.map(f=>({...f,st:touched[f.path]||null,...churnOf(f.path)}));
  const maxLog=Math.log2(Math.max(2,...fileRecs.map(f=>Math.max(f.size,1))));
  const tiles=new THREE.InstancedMesh(
    new THREE.BoxGeometry(1,1,1),
    new THREE.MeshStandardMaterial({roughness:0.85,metalness:0}),
    fileRecs.length);
  const tileIds=[],mtx=new THREE.Matrix4();
  fileRecs.forEach((f,i)=>{
    const sx=Math.max(f.rect.w,MIN_TILE),sz=Math.max(f.rect.h,MIN_TILE);
    mtx.compose(
      new THREE.Vector3(f.rect.x+f.rect.w/2-cx,TILE_H/2,f.rect.y+f.rect.h/2-cz),
      new THREE.Quaternion(),new THREE.Vector3(sx,TILE_H,sz));
    tiles.setMatrixAt(i,mtx);
    tiles.setColorAt(i,tileColor(f.path));
    tileIds.push(i);
  });
  tiles.instanceMatrix.needsUpdate=true;
  if(tiles.instanceColor)tiles.instanceColor.needsUpdate=true;
  group.add(tiles);

  /* terrain: the columns that carry the reading.
     churn mode — only what the commit touched rises, by lines changed;
     size mode  — every file rises by bytes, touched ones keep their hue. */
  const risers=heightMode==='churn'
    ?fileRecs.map((f,i)=>({f,i})).filter(({f})=>f.st)
    :fileRecs.map((f,i)=>({f,i}));
  if(risers.length){
    /* the one emissive material in the scene: Space Walk's attention
       terrain is MeshBasic + toneMapped:false precisely so lit columns
       read as light rather than as well-lit geometry. No vertexColors:
       that flag makes the shader demand a geometry colour attribute
       BoxGeometry does not carry (every instance renders black); the
       per-instance colour rides instanceColor, set by setColorAt. */
    const terrain=new THREE.InstancedMesh(
      new THREE.BoxGeometry(1,1,1),
      new THREE.MeshBasicMaterial({toneMapped:false}),
      risers.length);
    const terrainIds=[];
    risers.forEach(({f,i},n)=>{
      let h,col;
      if(heightMode==='churn'){
        h=f.binary?CHURN_MIN_H+3:churnHeight(f.lines);
        col=new THREE.Color(f.binary?BINARY:TOUCH[f.st]||TOUCH.M);
      }else{
        const t=locFraction(f.size,maxLog);
        h=LOC_MIN_H+Math.pow(t,LOC_HEIGHT_GAMMA)*(LOC_MAX_H-LOC_MIN_H);
        col=f.st?new THREE.Color(TOUCH[f.st]):rampColor(t);
      }
      const sx=Math.max(f.rect.w,MIN_TILE)*0.92,sz=Math.max(f.rect.h,MIN_TILE)*0.92;
      mtx.compose(
        new THREE.Vector3(f.rect.x+f.rect.w/2-cx,h/2,f.rect.y+f.rect.h/2-cz),
        new THREE.Quaternion(),new THREE.Vector3(sx,h,sz));
      terrain.setMatrixAt(n,mtx);
      terrain.setColorAt(n,col);
      terrainIds.push(i);
    });
    terrain.instanceMatrix.needsUpdate=true;
    if(terrain.instanceColor)terrain.instanceColor.needsUpdate=true;
    group.add(terrain);
    group.userData.terrain=terrain;
    group.userData.terrainIds=terrainIds;
  }
  group.userData.tiles=tiles;
  group.userData.tileIds=tileIds;

  labelSet=new LabelSet(world.dirs.filter(d=>
    d.depth>=1&&d.fileCount>0&&d.rect.w>0&&d.rect.h>0),group,cx,cz);

  cityGroup=group;
  kit.scene.add(group);
  frameCamera();
}

/* god-view framing: the canonical direction, distance fitted to the plain */
function frameCamera(){
  const{camera,controls}=sceneKit;
  const dir=new THREE.Vector3(...VIEW_DIR).normalize();
  const half=WORLD/2;
  const tallest=heightMode==='churn'?CHURN_MAX_H:LOC_MAX_H;
  const pts=[
    new THREE.Vector3(-half,0,-half),new THREE.Vector3(half,0,-half),
    new THREE.Vector3(-half,0,half),new THREE.Vector3(half,0,half),
    new THREE.Vector3(0,tallest,0),
  ];
  const forward=dir.clone().negate();
  const right=new THREE.Vector3().crossVectors(forward,new THREE.Vector3(0,1,0)).normalize();
  const up=new THREE.Vector3().crossVectors(right,forward);
  const tanV=Math.tan(THREE.MathUtils.degToRad(camera.fov)/2);
  const tanH=tanV*Math.max(camera.aspect,0.2);
  let dist=0;
  for(const p of pts){
    const depth=p.dot(forward);
    dist=Math.max(dist,Math.abs(p.dot(right))/tanH-depth,Math.abs(p.dot(up))/tanV-depth);
  }
  dist=Math.max(dist*0.86,WORLD*0.36);
  controls.target.set(0,0,0);
  camera.position.copy(dir.multiplyScalar(dist));
  camera.lookAt(0,0,0);
  controls.update();
}

/* ---------------- district labels (DirLabelSet's rules) ---------------- */

function labelTexture(text){
  const font='500 30px system-ui,sans-serif';
  const measure=document.createElement('canvas').getContext('2d');
  measure.font=font;
  const width=Math.ceil(measure.measureText(text).width)+24,height=44;
  const cv=document.createElement('canvas');
  cv.width=width*2;cv.height=height*2;
  const ctx=cv.getContext('2d');
  ctx.scale(2,2);ctx.font=font;
  ctx.textBaseline='middle';ctx.textAlign='center';
  ctx.fillStyle='rgba(233,228,217,0.95)';
  ctx.fillText(text,width/2,height/2+1);
  const tex=new THREE.CanvasTexture(cv);
  tex.anisotropy=4;
  return{tex,aspect:width/height};
}

/* A label shows while its subtree spans enough screen pixels; of two
   colliding labels the one naming fewer files drops. Reprojected only
   when the camera actually moved. */
class LabelSet{
  constructor(dirs,group,cx,cz){
    this.items=[];
    this.lastPos=new THREE.Vector3(Infinity,Infinity,Infinity);
    this.lastW=0;this.lastH=0;
    const budget=[...dirs].sort((a,b)=>b.fileCount-a.fileCount).slice(0,LABEL_BUDGET);
    for(const d of budget){
      const{tex,aspect}=labelTexture(d.name);
      const sprite=new THREE.Sprite(new THREE.SpriteMaterial({
        map:tex,transparent:true,opacity:0,depthWrite:false,depthTest:false,
        toneMapped:false,fog:false}));
      sprite.renderOrder=20;
      sprite.visible=false;
      sprite.position.set(d.rect.x+d.rect.w/2-cx,LABEL_Y,d.rect.y+d.rect.h/2-cz);
      sprite.raycast=()=>undefined;
      group.add(sprite);
      this.items.push({sprite,d,aspect,radius:Math.hypot(d.rect.w,d.rect.h)/2,target:0});
    }
  }
  /* Reproject only when the camera or viewport actually moved, then pick
     the labels whose subtree is prominent on screen. Type is a constant
     PIXEL height (map labels never grow with zoom), a district you are
     inside drops its name, and of two colliding labels the one naming
     fewer files loses. */
  update(camera,vw,vh){
    if(!vw||!vh)return;
    const moved=camera.position.distanceToSquared(this.lastPos)>0.01
      ||vw!==this.lastW||vh!==this.lastH;
    if(moved){
      this.lastPos.copy(camera.position);this.lastW=vw;this.lastH=vh;
      const tanV=Math.tan(THREE.MathUtils.degToRad(camera.fov)/2);
      const maxDim=Math.max(vw,vh);
      const cands=[];
      const pt=new THREE.Vector3();
      for(const it of this.items){
        pt.copy(it.sprite.position);
        const dist=pt.distanceTo(camera.position);
        const pxPerWorld=vh/(2*dist*tanV);
        const subtreePx=it.radius*pxPerWorld;
        pt.project(camera);
        const sx=((pt.x+1)/2)*vw,sy=((1-pt.y)/2)*vh;
        const onScreen=pt.z<1&&sx>-60&&sx<vw+60&&sy>-40&&sy<vh+40;
        if(!onScreen||subtreePx<LABEL_MIN_SUBTREE_PX||subtreePx>maxDim*1.6){
          it.target=0;continue;
        }
        const ph=it.d.depth<=1?15:13;      /* constant screen-size type */
        const worldH=ph/pxPerWorld;
        it.sprite.scale.set(worldH*it.aspect,worldH,1);
        cands.push({it,sx,sy,pw:ph*it.aspect,ph});
      }
      cands.sort((a,b)=>b.it.d.fileCount-a.it.d.fileCount);
      const kept=[];
      for(const c of cands){
        const clash=kept.some(o=>
          Math.abs(o.sx-c.sx)<(o.pw+c.pw)/2+14&&
          Math.abs(o.sy-c.sy)<(o.ph+c.ph)/2+10);
        c.it.target=clash?0:1;
        if(!clash)kept.push(c);
      }
    }
    /* ease toward the LOD targets so names fade rather than pop */
    for(const it of this.items){
      const m=it.sprite.material;
      const diff=it.target-m.opacity;
      if(Math.abs(diff)>0.02)m.opacity+=diff*0.16;
      else m.opacity=it.target;
      it.sprite.visible=m.opacity>0.02;
    }
  }
}

/* ---------------- hover card ---------------- */

function setHover(rec,ev){
  if(!rec){hideHC();return;}
  if(hoverId!==rec.path){
    hoverId=rec.path;
    const word={A:'added',M:'modified',R:'renamed'}[rec.st];
    const c=(snap.churn||{})[rec.path];
    const churnLine=rec.st
      ?(rec.binary?'binary · lines not countable'
        :c?`<b class="tAdd">+${(c.added||0).toLocaleString()}</b>
            <b class="tDel">&minus;${(c.deleted||0).toLocaleString()}</b> lines`
          :'no line change')
      :'untouched by this commit';
    hc.innerHTML=`<code>${esc(rec.path)}</code>
      <span>${fmtBytes(rec.size)}${word?' · '+word:''}</span>
      <span class="hc-churn">${churnLine}</span>`;
    hc.classList.add('is-on');
  }
  if(ev){
    const r=hc.getBoundingClientRect();
    let x=ev.clientX+16,y=ev.clientY+16;
    if(x+r.width>innerWidth-8)x=ev.clientX-r.width-16;
    if(y+r.height>innerHeight-8)y=ev.clientY-r.height-16;
    hc.style.left=x+'px';hc.style.top=y+'px';
  }
}
function hideHC(){hoverId=null;hc?.classList.remove('is-on');}

const fmtBytes=n=>n<1024?n+' B'
  :n<1048576?(n/1024).toFixed(n<10240?1:0)+' KB':(n/1048576).toFixed(1)+' MB';
