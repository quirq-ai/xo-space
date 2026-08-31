/* q5 · Watcher — "The Core".
   The quirq watcher as a breathing plasma core: three layered glow pulses
   breathing out of phase, ringed by two counter-rotating arc wisps. Breath
   tempo encodes liveness — a fresh heartbeat (<1h) breathes fast and bright,
   a stale one slow and dim. Every .quirq file is a firefly on one of two
   shells (state inner, watcher outer) at its fnv(path) angle, bloom by age;
   lock files wear a thin stroked ring around a normal ember (keyed in the
   expanded footer). Each file also owns a dust trail from the core out to
   its shell — the watcher feeding its files — dust brightness inherits the
   file's freshness; a few motes drift outward along it at heartbeat tempo.
   Shell rings, ambient halo and the dust trails are baked into a static
   base at init; only the core, wisps, drifting motes and the handful of
   fireflies animate per frame.
   Expanded: ring captions sit in a gap at 12 o'clock ON their own ring
   (the q3 convention), hash-suffixed names compress to "todos.json · d677",
   and the LIVE/IDLE readout docks directly beneath the core over a scrim,
   like the hero figures on q2/q3. */
import {TAU,INK,INK2,INK3,MONO,SERIF,hexA,tint,fnv,glowSprite,drawGlow,
  withAdditive,ember,text,layer,ageLabel}from'../lib.js';

const wrap=a=>{a%=TAU;return a<0?a+TAU:a;};
const adist=(a,c)=>wrap(a-c+Math.PI)-Math.PI;   /* signed shortest arc */
const bright=s=>s==null?.3:s<3600?.95:s<86400?.55:.3;
/* "todos.json.d6779198.lock" -> "todos.json · d677" */
const pretty=n=>{
  const m=String(n).match(/^(.+?)\.([0-9a-f]{6,})(\.lock)?$/i);
  return m?m[1]+' · '+m[2].slice(0,4):String(n);
};

/* deterministic angular layout: push out of avoid sectors, then relax to a
   minimum gap so fnv-coincident files never overlap */
function spread(grp,minGap,avoid){
  const shun=()=>{for(const o of grp)for(const [c,w] of avoid){
    const d=adist(o.a,c);
    if(Math.abs(d)<w)o.a=wrap(c+(d>=0?w:-w));
  }};
  shun();
  if(grp.length<2)return;
  for(let pass=0;pass<3;pass++){
    grp.sort((p,q)=>p.a-q.a);
    for(let i=0;i<grp.length;i++){
      const p=grp[i],q=grp[(i+1)%grp.length];
      const gap=i===grp.length-1?wrap(q.a-p.a):q.a-p.a;
      if(gap<minGap){
        const push=(minGap-gap)/2;
        p.a=wrap(p.a-push);q.a=wrap(q.a+push);
      }
    }
    shun();
  }
}

function wisp(gc,cx,cy,r,rot,span,color){
  gc.lineCap='round';
  gc.beginPath();gc.arc(cx,cy,r,rot,rot+span);
  gc.strokeStyle=hexA(color,.09);gc.lineWidth=3.6;gc.stroke();
  gc.beginPath();gc.arc(cx,cy,r,rot,rot+span);
  gc.strokeStyle=hexA(color,.22);gc.lineWidth=1.2;gc.stroke();
}

export default{
  kind:'watcher',

  init(data,env){
    const {W,H,dpr,color,expanded}=env;
    const files=(data&&data.files)||[];
    const newest=data&&data.newestAgeSec!=null?data.newestAgeSec:null;
    const live=newest!=null&&newest<3600;
    const cx=W/2,cy=H/2,m=Math.min(W,H);
    const coreR=m*.075;
    const shellR={state:m*.26,watcher:m*.385};
    const sprite=glowSprite(color);

    const flies=files.map(f=>({f,
      shell:f.group==='watcher'?'watcher':'state',
      a:wrap(fnv(f.path)*TAU),
      lock:String(f.name).includes('.lock'),
      label:pretty(f.name),
      b:bright(f.ageSec),
      p1:fnv(f.path+'/x')*TAU,p2:fnv(f.path+'/y')*TAU,
      tw:fnv(f.path+'/tw')*TAU}));

    const base=layer(W,H,dpr);
    const g=base.g;

    /* expanded ring captions live in a gap at 12 o'clock on their own ring
       (same convention as q3's log orbit); measure so the gap fits the text */
    g.font=`400 8px ${MONO}`;
    const capW=s=>g.measureText(s).width+s.length*1.6;  /* + tracking */
    const gaps={
      state:expanded?(capW('STATE')/2+10)/shellR.state:0,
      watcher:expanded?(capW('WATCHER')/2+10)/shellR.watcher:0};

    for(const sh of ['state','watcher']){
      const grp=flies.filter(o=>o.shell===sh);
      const avoid=expanded?[[-Math.PI/2,gaps[sh]+.24]]:[];
      spread(grp,Math.min(.95,TAU/Math.max(1,grp.length)*.6),avoid);
    }
    for(const o of flies){
      o.r=shellR[o.shell];
      o.cos=Math.cos(o.a);o.sin=Math.sin(o.a);
      o.bx=cx+o.cos*o.r;o.by=cy+o.sin*o.r;
    }

    /* dust: each file owns a stream from the core out to its shell — a baked
       trail of static dust dots (density from trail length, brightness from
       the file's age) plus a few drifting motes as the motion accents */
    const motes=[];
    for(const o of flies){
      o.r0=coreR*1.5;o.len=o.r-6-o.r0;
      const n=Math.max(2,Math.round(o.len/(expanded?55:34)));
      for(let i=0;i<n&&motes.length<48;i++)motes.push({o,
        off:fnv(o.f.path+'/m'+i),
        j:(fnv(o.f.path+'/j'+i)-.5)*6,
        wp:fnv(o.f.path+'/w'+i)*TAU});
    }

    /* static base: a tightly clamped ambient seat for the core (corners stay
       the shared neutral dark), the two shell guide rings bright enough to
       structure the card at grid size, and the baked dust trails */
    withAdditive(g,()=>{
      drawGlow(g,sprite,cx,cy,m*.28,.06);
      g.lineCap='round';
      for(const sh of ['state','watcher']){
        const r=shellR[sh],gw=gaps[sh];
        const a0=-Math.PI/2+gw,a1=TAU-Math.PI/2-gw;
        g.beginPath();g.arc(cx,cy,r,a0,a1);
        g.strokeStyle=hexA(color,.10);g.lineWidth=3.2;g.stroke();
        g.beginPath();g.arc(cx,cy,r,a0,a1);
        g.strokeStyle=hexA(tint(color,.4),.32);g.lineWidth=1;g.stroke();
      }
      for(const o of flies){
        const dots=Math.max(4,Math.floor(o.len/(expanded?11:8)));
        const seat=.45+.55*o.b;
        for(let i=0;i<dots;i++){
          const f=i/(dots-1);
          const d=o.r0+o.len*f;
          const jit=(fnv(o.f.path+'/d'+i)-.5)*7;
          drawGlow(g,sprite,cx+o.cos*d-o.sin*jit,cy+o.sin*d+o.cos*jit,
            2.4+f*3,(.10+.26*f)*seat);
        }
      }
    });

    return {flies,motes,base,sprite,cx,cy,coreR,shellR,newest,live,
      lockCount:flies.filter(o=>o.lock).length,count:files.length};
  },

  draw(gc,s,env,t,mouse){
    const {W,H,color,expanded,reduced}=env;
    const px=mouse?(mouse.x-W/2)/W*6:0,py=mouse?(mouse.y-H/2)/H*6:0;
    gc.save();
    gc.translate(px,py);
    s.base.blit(gc);
    const {cx,cy,coreR}=s;

    /* breath tempo is the datum: <1h since last touch -> quick + bright */
    const P=s.live?3.2:8;
    const peak=s.live?1:.62;
    const ph=k=>reduced?.55:.5+.5*Math.sin(t*TAU/P+k);

    withAdditive(gc,()=>{
      /* counter-rotating arc wisps just outside the core */
      const w1=reduced?-.6:t*TAU/26,w2=reduced?2.2:-t*TAU/34;
      wisp(gc,cx,cy,coreR*2,w1,Math.PI*.92,color);
      wisp(gc,cx,cy,coreR*2.6,w2,Math.PI*.66,color);

      /* motes drifting core -> shell along each file's dust trail */
      const MP=s.live?11:22;
      for(const mt of s.motes){
        const f=reduced?mt.off:(t/MP+mt.off)%1;
        const o=mt.o;
        const d=o.r0+o.len*f;
        const wob=reduced?0:2*Math.sin(t*TAU/17+mt.wp);
        drawGlow(gc,s.sprite,cx+o.cos*d-o.sin*(mt.j+wob),
          cy+o.sin*d+o.cos*(mt.j+wob),
          2.6+f*2,(.16+.5*Math.sin(Math.PI*f))*(.45+.55*o.b));
      }

      /* three layered glows breathing out of phase + white-hot centre */
      const b1=ph(0),b2=ph(2.1),b3=ph(4.2);
      drawGlow(gc,s.sprite,cx,cy,coreR*(1.9+.5*b1),(.28+.26*b1)*peak);
      drawGlow(gc,s.sprite,cx,cy,coreR*(1.15+.3*b2),(.46+.3*b2)*peak);
      drawGlow(gc,s.sprite,cx,cy,coreR*(.6+.2*b3),(.68+.32*b3)*peak);
      gc.beginPath();gc.arc(cx,cy,coreR*.16+b3*1.4,0,TAU);
      gc.fillStyle=hexA(tint(color,.93),(.6+.4*b3)*Math.max(peak,.7));
      gc.fill();

      /* fireflies: one per .quirq file. A floor under size/alpha keeps all
         of them reading at grid scale; age still sets bloom + core weight.
         Locks are normal embers wearing a thin ring (keyed in the footer). */
      const er=expanded?3.4:3;
      for(const o of s.flies){
        const x=o.bx+(reduced?0:4*Math.sin(t*TAU/15.3+o.p1));
        const y=o.by+(reduced?0:4*Math.sin(t*TAU/12.7+o.p2));
        const twk=reduced?1:.88+.12*Math.sin(t*TAU/7.3+o.tw);
        const a=(.42+.58*o.b)*twk;
        drawGlow(gc,s.sprite,x,y,er*6.6,a*.22);      /* wide bloom seat */
        drawGlow(gc,s.sprite,x,y,er*3.4,a*.5);
        ember(gc,s.sprite,color,x,y,er*(.7+.55*o.b),a);
        if(o.lock){
          gc.beginPath();gc.arc(x,y,er*2.1,0,TAU);
          gc.strokeStyle=hexA(tint(color,.65),.2+.4*a);
          gc.lineWidth=1;gc.stroke();
        }
      }
      /* one travelling accent: the watcher's sweep along the outer shell,
         present only while the heartbeat is live (<1h) */
      if(s.live&&!reduced){
        const sa=t*TAU/21;
        const sx=cx+Math.cos(sa)*s.shellR.watcher,
          sy=cy+Math.sin(sa)*s.shellR.watcher;
        drawGlow(gc,s.sprite,sx,sy,expanded?9:6,.4);
        gc.lineCap='round';
        gc.beginPath();gc.arc(cx,cy,s.shellR.watcher,sa-.5,sa);
        gc.strokeStyle=hexA(tint(color,.4),.22);gc.lineWidth=1.2;gc.stroke();
      }
    });

    if(expanded){
      /* ring captions in their 12-o'clock gaps, each on its own ring */
      text(gc,'STATE',cx,cy-s.shellR.state+3,
        {font:`400 8px ${MONO}`,col:INK3,track:.16});
      text(gc,'WATCHER',cx,cy-s.shellR.watcher+3,
        {font:`400 8px ${MONO}`,col:INK3,track:.16});
      for(const o of s.flies){
        const nm=o.label.length>22?o.label.slice(0,21)+'…':o.label;
        let lx=o.bx+o.cos*13,ly=o.by+o.sin*10+3,align;
        if(o.cos>.3)align='left';
        else if(o.cos<-.3)align='right';
        else{align='center';ly+=o.sin>0?9:-7;}
        text(gc,nm,lx,ly,{font:`400 8.5px ${MONO}`,col:INK2,align,alpha:.85});
      }
      /* the LIVE/IDLE readout docks under the core like the q2/q3 numerals,
         seated on a soft scrim so the motes fade behind it */
      const ny=cy+coreR+44;
      const scr=gc.createRadialGradient(cx,ny,0,cx,ny,72);
      scr.addColorStop(0,'rgba(7,8,10,.72)');
      scr.addColorStop(1,'rgba(7,8,10,0)');
      gc.save();gc.translate(cx,ny);gc.scale(1,.42);gc.translate(-cx,-ny);
      gc.fillStyle=scr;gc.beginPath();gc.arc(cx,ny,72,0,TAU);gc.fill();
      gc.restore();
      text(gc,s.live?'LIVE':'IDLE',cx,ny+2,{font:`500 24px ${SERIF}`,col:INK});
      text(gc,s.count?ageLabel(s.newest)||'—':'NO FILES',cx,ny+17,
        {font:`400 8px ${MONO}`,col:INK3,track:.14});
      if(s.lockCount)                          /* key the ringed embers */
        text(gc,'○ RING = LOCK FILE · '+s.lockCount,cx,H-10,
          {font:`400 8px ${MONO}`,col:INK3,track:.14,alpha:.9});
    }
    gc.restore();
  },

  hits(s,env){
    const out=[{x:s.cx,y:s.cy,r:s.coreR*1.2,
      tip:{kick:'quirq watcher',title:s.live?'LIVE':'IDLE',
        rows:[['Files',String(s.count)],
          ['Locks',String(s.lockCount)],
          ['Newest',ageLabel(s.newest)]]}}];
    for(const o of s.flies){
      out.push({x:o.bx,y:o.by,r:14,
        tip:{kick:'.quirq · '+o.f.group+(o.lock?' · lock':''),title:o.label,
          rows:[['File',o.f.name],['Where',o.f.path],['Size',o.f.sizeLabel],
            ['Touched',ageLabel(o.f.ageSec)]]}});
    }
    return out;
  }
};
