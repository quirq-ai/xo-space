/* q8 · XO Data — "The Archive".
   One constellation instead of eight competing panels: every .xo tree the
   watcher maintains is a luminous cluster — the workspace tree is the hub
   at center, the per-project trees ride a single elliptical orbit around
   it, hairline spokes tying each back to the hub. Cluster radius encodes
   log(total bytes), so the ~2000x span from site (234 B) to the workspace
   tree stays readable; every dot inside a cluster is one real file placed
   on a deterministic golden-angle spiral (dot size = log bytes, brightness
   = freshness of last write with a visibility floor, directories as tiny
   hollow rings). Each cluster's largest file sits at its core as a
   breathing ember; a single spark walks the spokes hub→project as the one
   travelling accent. Orbit, spokes, file dots and all expanded labels are
   baked into a static layer at init — only ~9 cluster halos, ~9 embers
   and the spark animate. Grid mode is textless (the caption bar carries
   label+stat); expanded mode adds direct labels (tree name + bytes), a
   footer key, and hits() tooltips for every tree and file. Trees beyond
   11 and files beyond 14 per tree merge into honest "+n more" entries. */
import {TAU,INK2,INK3,MONO,hexA,tint,fnv,freshness,glowSprite,drawGlow,
  withAdditive,ember,softLine,softRing,text,layer,fmtDate}from'../lib.js';

const GOLD=2.399963229728653;   /* golden angle — file spiral in a cluster */
const SPARK_S=4;                /* seconds per spark hop hub -> project */
const lg=b=>Math.log2(1+Math.max(0,b||0));
const fmtBytes=b=>b>=1048576?(b/1048576).toFixed(1)+' MB'
  :b>=1024?Math.round(b/1024)+' KB':b+' B';
const visFresh=d=>Math.max(.3,freshness(d)); /* floor: cold ≠ invisible */

/* faint elliptical orbit path — softRing stretched to (rx,ry) */
function softEllipse(g,cx,cy,rx,ry,color,w,a){
  g.save();g.translate(cx,cy);g.scale(1,ry/rx);
  g.strokeStyle=hexA(color,a*.22);g.lineWidth=w*3;
  g.beginPath();g.arc(0,0,rx,0,TAU);g.stroke();
  g.strokeStyle=hexA(tint(color,.35),a);g.lineWidth=w;
  g.beginPath();g.arc(0,0,rx,0,TAU);g.stroke();
  g.restore();
}

/* group -> tree: files sorted big-first; sub-threshold tail merges into
   one "+n more" entry so no sliver ever renders unlabeled */
function mkTree(gp,maxFiles){
  const raw=(gp.files||[]).slice()
    .sort((a,b)=>(b.dir?-1:b.size||0)-(a.dir?-1:a.size||0));
  let files=raw;
  if(raw.length>maxFiles){
    const rest=raw.slice(maxFiles-1);
    files=raw.slice(0,maxFiles-1);
    files.push({name:'+'+rest.length+' more',
      size:rest.reduce((s,f)=>s+(f.size||0),0),
      sizeLabel:fmtBytes(rest.reduce((s,f)=>s+(f.size||0),0)),
      date:rest.map(f=>f.date||'').sort().pop()||null,merged:true});
  }
  return {label:gp.label||'—',files,
    total:(gp.files||[]).reduce((s,f)=>s+(f.size||0),0),
    newest:(gp.files||[]).map(f=>f.date||'').sort().pop()||null,
    count:(gp.files||[]).length};
}

export default{
  kind:'treemap',

  init(data,env){
    const {W,H,dpr,color,expanded}=env;
    const unit=Math.min(W,H);
    const cx=W/2,cy=H/2;
    const sprite=glowSprite(color);
    const base=layer(W,H,dpr);
    const g=base.g;
    const groupsIn=(data&&data.groups)||[];

    /* honest empty state: the hub and orbit stand faint and labeled */
    if(!groupsIn.length){
      softRing(g,cx,cy,unit*.13,color,1,.16);
      softEllipse(g,cx,cy,Math.min(W*.38,unit*.42),unit*.30,color,1,.10);
      text(g,'NO .XO OUTPUT YET',cx,cy+3,
        {font:`400 8px ${MONO}`,col:INK3,track:.16});
      return {clusters:[],nodes:[],base,sprite,cx,cy};
    }

    const all=groupsIn.map(gp=>mkTree(gp,14));
    /* hub = the workspace tree (fall back to the largest tree) */
    let hubI=all.findIndex(tr=>tr.label.toLowerCase()==='workspace');
    if(hubI<0)hubI=all.reduce((b,tr,i)=>tr.total>all[b].total?i:b,0);
    const hub=all[hubI];
    let sats=all.filter((_,i)=>i!==hubI);
    if(sats.length>11){
      sats.sort((a,b)=>b.total-a.total);
      const rest=sats.slice(10);
      sats=sats.slice(0,10);
      sats.push({label:'+'+rest.length+' trees',files:[],
        total:rest.reduce((s,tr)=>s+tr.total,0),
        newest:rest.map(tr=>tr.newest||'').sort().pop()||null,
        count:rest.reduce((s,tr)=>s+tr.count,0)});
    }

    const maxLg=Math.max(1,...all.map(tr=>lg(tr.total)));
    const maxFileLg=Math.max(1,
      ...all.flatMap(tr=>tr.files.map(f=>lg(f.size))));
    const norm=tr=>lg(tr.total)/maxLg;   /* bytes on a log scale, keyed */

    const rcSat=n=>unit*(expanded?.040+.062*n:.048+.072*n);
    const rcHub=unit*(expanded?.072+.076*norm(hub):.076+.074*norm(hub));
    const rcMax=sats.length?Math.max(...sats.map(tr=>rcSat(norm(tr)))):unit*.1;
    const labM=expanded?36:6;            /* room for expanded orbit labels */
    let ry=Math.max(H*.435-rcMax-labM,rcHub+rcMax+8);
    let rx=Math.min(W*.435-rcMax-(expanded?100:8),ry*(expanded?1.7:1.45));
    rx=Math.max(rx,ry*.6);

    const nodes=sats.map((tr,i)=>{
      const a=-Math.PI/2+(i+.5)*TAU/Math.max(1,sats.length)
        +(fnv('arc'+tr.label)-.5)*.10;
      const n=norm(tr);
      return {tr,a,n,rc:rcSat(n),x:cx+Math.cos(a)*rx,y:cy+Math.sin(a)*ry,
        phase:fnv('ph'+tr.label)*TAU,fresh:visFresh(tr.newest)};
    });
    const hubNode={tr:hub,a:0,n:norm(hub),rc:rcHub,x:cx,y:cy,
      phase:fnv('ph'+hub.label)*TAU,fresh:visFresh(hub.newest)};
    const clusters=[hubNode,...nodes];

    /* files on a golden-angle spiral; the largest file holds the core */
    for(const nd of clusters){
      const m=nd.tr.files.length;
      nd.dots=nd.tr.files.map((f,i)=>{
        const rr=i===0?0:nd.rc*.92*Math.sqrt(i/Math.max(1,m-1));
        const da=i*GOLD+fnv('rot'+nd.tr.label)*TAU;
        const szn=lg(f.size)/maxFileLg;
        return {f,x:nd.x+Math.cos(da)*rr,y:nd.y+Math.sin(da)*rr,
          r:f.dir?2.2:(1.0+2.9*szn)*(expanded?1.8:1),
          a:.30+.58*visFresh(f.date)};
      });
      nd.ember=nd.dots.length?nd.dots[0]:null;
    }

    /* ---- bake the static base: orbit, spokes, dots ---- */
    withAdditive(g,()=>{
      if(nodes.length)softEllipse(g,cx,cy,rx,ry,color,1,expanded?.14:.11);
      for(const nd of nodes){
        const dx=nd.x-cx,dy=nd.y-cy,L=Math.hypot(dx,dy)||1;
        softLine(g,cx+dx/L*rcHub*1.05,cy+dy/L*rcHub*1.05,
          nd.x-dx/L*nd.rc*.95,nd.y-dy/L*nd.rc*.95,color,.8,.16);
      }
      for(const nd of clusters){
        drawGlow(g,sprite,nd.x,nd.y,nd.rc*1.7,.08+.13*nd.n); /* volume glow */
        for(const dt of nd.dots){
          if(dt===nd.ember)continue;            /* the ember draws live */
          if(dt.f.dir){softRing(g,dt.x,dt.y,dt.r,color,.8,.4);continue;}
          drawGlow(g,sprite,dt.x,dt.y,dt.r*3,dt.a*.55);
          g.beginPath();g.arc(dt.x,dt.y,dt.r*.62,0,TAU);
          g.fillStyle=hexA(tint(color,.75),dt.a);g.fill();
        }
      }
    });

    /* ---- expanded: direct labels + footer key (grid stays textless) ---- */
    if(expanded){
      g.font=`400 9px ${MONO}`;
      for(const nd of nodes){
        const dx=nd.x-cx,dy=nd.y-cy,L=Math.hypot(dx,dy)||1;
        const ux=dx/L,uy=dy/L;
        const name=nd.tr.label.toUpperCase();
        const w=g.measureText(name).width+name.length*1.0; /* + tracking */
        let lx=nd.x+ux*(nd.rc+12),ly=nd.y+uy*(nd.rc+16);
        const align=ux>.35?'left':ux<-.35?'right':'center';
        if(uy<-.35)ly-=13;else if(uy>.35)ly+=6;
        const lo=align==='left'?0:align==='center'?w/2:w;
        const hi=align==='right'?0:align==='center'?w/2:w;
        lx=Math.min(Math.max(lx,12+lo),W-12-hi);
        ly=Math.min(Math.max(ly,20),H-26);
        text(g,name,lx,ly,
          {font:`400 9px ${MONO}`,col:INK2,align,track:.10,alpha:.9});
        text(g,fmtBytes(nd.tr.total)+' · '+nd.tr.count+' FILES',lx,ly+11,
          {font:`400 8px ${MONO}`,col:INK3,align,alpha:.85});
      }
      /* hub sublabel under the hub on a soft scrim */
      const hy=cy+rcHub+20;
      const scr=g.createRadialGradient(cx,hy,0,cx,hy,64);
      scr.addColorStop(0,'rgba(7,8,10,.72)');
      scr.addColorStop(1,'rgba(7,8,10,0)');
      g.save();g.translate(cx,hy);g.scale(1,.4);g.translate(-cx,-hy);
      g.fillStyle=scr;g.beginPath();g.arc(cx,hy,64,0,TAU);g.fill();
      g.restore();
      text(g,hub.label.toUpperCase(),cx,hy,
        {font:`400 9px ${MONO}`,col:INK2,track:.12});
      text(g,fmtBytes(hub.total)+' · '+hub.count+' FILES',cx,hy+12,
        {font:`400 8px ${MONO}`,col:INK3,alpha:.85});
      text(g,'CLUSTER = .XO TREE · SIZE = BYTES (LOG) · GLOW = RECENCY',
        cx,H-10,{font:`400 8px ${MONO}`,col:INK3,track:.12,alpha:.75});
    }

    return {clusters,nodes,hubNode,base,sprite,cx,cy};
  },

  draw(gc,s,env,t,mouse){
    const {W,H,color,expanded,reduced}=env;
    const px=(!expanded&&mouse)?(mouse.x-W/2)/W*6:0;
    const py=(!expanded&&mouse)?(mouse.y-H/2)/H*6:0;
    gc.save();
    gc.translate(px,py);
    s.base.blit(gc);
    if(s.clusters.length)withAdditive(gc,()=>{
      for(const nd of s.clusters){
        const breath=reduced?.6:.5+.5*Math.sin(t*TAU/7+nd.phase);
        /* breathing cluster halo — intensity scales with tree bytes */
        drawGlow(gc,s.sprite,nd.x,nd.y,nd.rc*(1.25+.2*breath),
          (.10+.20*nd.n)*(.55+.45*breath));
        if(nd.ember){
          const eb=nd.ember;
          const a=(.45+.55*breath)*(.4+.6*nd.fresh);
          drawGlow(gc,s.sprite,eb.x,eb.y,nd.rc*.62,a*.8);
          ember(gc,s.sprite,color,eb.x,eb.y,eb.r*.7+.6*breath,
            Math.min(1,a+.25));
        }
      }
      /* one spark walks a spoke hub -> project, cycling the orbit */
      if(!reduced&&s.nodes.length){
        const nd=s.nodes[Math.floor(t/SPARK_S)%s.nodes.length];
        const f=(t%SPARK_S)/SPARK_S;
        const dx=nd.x-s.cx,dy=nd.y-s.cy,L=Math.hypot(dx,dy)||1;
        const f0=s.hubNode.rc*1.05/L,f1=1-nd.rc*.95/L;
        const ff=f0+(f1-f0)*f;
        drawGlow(gc,s.sprite,s.cx+dx*ff,s.cy+dy*ff,4.5,
          Math.sin(f*Math.PI)*.5);
      }
    });
    gc.restore();
  },

  hits(s){
    const out=[];
    for(const nd of s.clusters)
      for(const dt of nd.dots)
        out.push({x:dt.x,y:dt.y,r:Math.max(8,dt.r+3),
          tip:{kick:'.xo output · '+nd.tr.label,title:dt.f.name,
            sub:dt.f.dir?'directory':(dt.f.merged?'merged small files':undefined),
            rows:[['Size',dt.f.sizeLabel||fmtBytes(dt.f.size||0)],
              ['Modified',fmtDate(dt.f.date)]]}});
    for(const nd of s.clusters)
      out.push({x:nd.x,y:nd.y,r:nd.rc+8,
        tip:{kick:'q8 · xo data',title:nd.tr.label+' · .xo tree',
          rows:[['Files',String(nd.tr.count)],
            ['Size',fmtBytes(nd.tr.total)],
            ['Last write',fmtDate(nd.tr.newest)]]}});
    return out;
  }
};
