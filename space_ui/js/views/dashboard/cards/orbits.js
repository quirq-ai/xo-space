/* q2 · Agent Sessions — "Session Rings".
   Three concentric luminous wobble-rings, one per session archive source:
   inner .claude, middle .cursor, outer project .xo — each ring's name is set
   directly on its own arc (upper-left) so the encoding is self-evident. Each
   ring is a ~110-point polyline whose radius breathes under two slow
   fnv-phased sinusoids; ring brightness follows the freshness of its newest
   archive. Every session is a comet bead sitting ON its ring at an fnv(name)
   angle, drifting one rev per ~120 s: bead size = sqrt(record count),
   brightness = lib.freshness(date), with a five-dot fading tail. Expanded
   mode labels a defined subset — the two freshest archives per ring —
   middle-ellipsised so the distinguishing tail of long paths survives, with
   vertical stagger + leader lines when two labels would collide; every bead
   still answers on hover. A breathing nucleus anchors the center, scaled up
   at grid size so the rings visibly orbit a focal core. Faint mean-radius
   tracks are baked into a static layer; only rings, beads, core animate. */
import {TAU,INK,INK2,INK3,MONO,SERIF,hexA,tint,fnv,glowSprite,drawGlow,
  withAdditive,ember,text,layer,fmtDate,freshness}from'../lib.js';

const DRIFT=TAU/120;      /* bead orbit: one revolution per ~120 s */
const RING_PTS=110;
const LINE_H=11;          /* label stagger line-height */

/* wobbled radius of a ring at polar angle th, time t — two slow
   low-frequency sinusoids (~18 s and ~26 s), phases fnv-seeded per ring */
function ringR(rg,th,t){
  return rg.R
    +rg.amp*Math.sin(rg.k1*th+rg.p1+t*TAU/18)
    +rg.amp*Math.sin(rg.k2*th+rg.p2-t*TAU/26);
}

/* middle-ellipsis: keep the head AND the distinguishing tail of long
   archive paths ("-home-…-mindwalk-web-src", never two identical stubs) */
const midTrunc=(nm,max=22)=>nm.length<=max?nm:nm.slice(0,6)+'…'+nm.slice(7-max);

/* set a short label on a ring arc, glyph by glyph, centred on thMid —
   the canvas equivalent of textPath, so each name visibly rides its ring.
   flip=true keeps bottom-half labels upright; a dark glyph backing keeps
   them legible when luminous strokes pass beneath */
function arcText(gc,str,cx,cy,r,thMid,font,col,alpha,flip){
  gc.save();
  gc.font=font;gc.fillStyle=col;gc.globalAlpha=alpha;gc.textAlign='center';
  gc.lineJoin='round';gc.lineWidth=3;gc.strokeStyle='rgba(7,8,10,.6)';
  const w=[...str].map(ch=>gc.measureText(ch).width+1.2);
  const dirn=flip?-1:1;
  let th=thMid-dirn*w.reduce((a,b)=>a+b,0)/(2*r);
  for(let i=0;i<str.length;i++){
    th+=dirn*w[i]/(2*r);
    gc.save();
    gc.translate(cx+Math.cos(th)*r,cy+Math.sin(th)*r);
    gc.rotate(th+(flip?-Math.PI/2:Math.PI/2));
    gc.strokeText(str[i],0,0);
    gc.fillText(str[i],0,0);
    gc.restore();
    th+=dirn*w[i]/(2*r);
  }
  gc.restore();
}

export default{
  kind:'orbits',

  init(data,env){
    const {W,H,dpr,color,expanded}=env;
    const cx=W/2,cy=H/2;
    const R3=Math.min(W,H)*.395;
    const RADII=[R3*.44,R3*.72,R3];
    const sprite=glowSprite(color);
    const src=(data&&data.rings)||[];

    const rings=[0,1,2].map(i=>{
      const r=src[i]||{id:'ring'+i,label:'—',items:[]};
      const R=RADII[i];
      const items=r.items||[];
      const maxFresh=items.length?Math.max(...items.map(it=>freshness(it.date))):0;
      const rg={id:r.id,label:r.label,R,
        amp:Math.min(3,R*.04),
        k1:2+Math.floor(fnv(r.id+':k1')*3),   /* 2..4 lobes */
        k2:5+Math.floor(fnv(r.id+':k2')*3),   /* 5..7 lobes */
        p1:fnv(r.id+':p1')*TAU,p2:fnv(r.id+':p2')*TAU,
        dir:i===1?-1:1,                        /* middle ring counter-drifts */
        lineA:(expanded?.38:.32)+.32*maxFresh,
        beads:null};
      rg.beads=items.map(it=>({
        name:it.name,count:it.count||0,date:it.date,ring:rg,
        a0:fnv(r.id+'|'+it.name)*TAU,
        r:2.2+Math.sqrt(it.count||0)*1.3,
        fresh:freshness(it.date),
        sx:cx,sy:cy}));
      /* the ring's name anchors mid-way across its largest bead gap and
         drifts in formation with the beads, so it never collides with them */
      const angs=rg.beads.map(b=>b.a0).sort((a,b)=>a-b);
      rg.anchor0=-2.0;
      if(angs.length){
        let bestGap=0;
        for(let j=0;j<angs.length;j++){
          const a=angs[j],b2=j+1<angs.length?angs[j+1]:angs[0]+TAU;
          if(b2-a>bestGap){bestGap=b2-a;rg.anchor0=(a+b2)/2;}
        }
      }
      return rg;
    });
    const beads=rings.flatMap(rg=>rg.beads);

    /* labeled subset follows one rule: the two freshest archives per ring
       (record count breaks ties) — the rule is noted on the canvas */
    const notable=rings.flatMap(rg=>rg.beads.slice()
      .sort((a,b)=>(b.fresh-a.fresh)||(b.count-a.count)).slice(0,2));

    /* static base: faint mean-radius tracks + center anchor haze */
    const base=layer(W,H,dpr);
    const g=base.g;
    withAdditive(g,()=>{
      for(const rg of rings){
        g.beginPath();g.arc(cx,cy,rg.R,0,TAU);
        g.strokeStyle=hexA(color,.05);g.lineWidth=5;g.stroke();
      }
      drawGlow(g,sprite,cx,cy,expanded?26:Math.min(W,H)*.18,.22);
    });

    return {rings,beads,notable,base,sprite,cx,cy};
  },

  draw(gc,s,env,t,mouse){
    const {W,H,color,expanded}=env;
    const px=mouse?(mouse.x-W/2)/W*6:0,py=mouse?(mouse.y-H/2)/H*6:0;
    const cx=s.cx+px,cy=s.cy+py;

    gc.save();gc.translate(px,py);s.base.blit(gc);gc.restore();

    withAdditive(gc,()=>{
      /* the three wobble-rings — wide faint pass under a thin bright core */
      for(const rg of s.rings){
        gc.beginPath();
        for(let i=0;i<=RING_PTS;i++){
          const th=i/RING_PTS*TAU;
          const r=ringR(rg,th,t);
          const x=cx+Math.cos(th)*r,y=cy+Math.sin(th)*r;
          i?gc.lineTo(x,y):gc.moveTo(x,y);
        }
        gc.closePath();
        gc.strokeStyle=hexA(color,rg.lineA*.26);gc.lineWidth=4.5;gc.stroke();
        gc.strokeStyle=hexA(tint(color,.4),rg.lineA);gc.lineWidth=1.2;gc.stroke();
      }

      /* session beads riding their ring, with comet tails trailing behind */
      for(const b of s.beads){
        const rg=b.ring;
        const th=b.a0+rg.dir*t*DRIFT;
        const rr=ringR(rg,th,t);
        const x=cx+Math.cos(th)*rr,y=cy+Math.sin(th)*rr;
        b.sx=x;b.sy=y;
        const step=(3+b.r*1.05)/rg.R;
        for(let k=5;k>=1;k--){
          const ta=th-rg.dir*k*step;
          const tr=ringR(rg,ta,t);
          drawGlow(gc,s.sprite,cx+Math.cos(ta)*tr,cy+Math.sin(ta)*tr,
            b.r*(1.7-k*.24),(.08+b.fresh*.5)*(1-k/6.5));
        }
        ember(gc,s.sprite,color,x,y,b.r,.2+.8*b.fresh);
      }

      /* breathing nucleus (~9 s) — a real focal core at grid size, kept
         modest in expanded mode so the hero numeral below stays readable */
      const breath=.85+.15*Math.sin(t*TAU/9);
      const coreR=expanded?18:Math.max(20,Math.min(W,H)*.13);
      drawGlow(gc,s.sprite,cx,cy,coreR*breath,expanded?.42:.6);
      drawGlow(gc,s.sprite,cx,cy,coreR*.42,expanded?.55:.7);
      gc.beginPath();gc.arc(cx,cy,expanded?1.5:2.2,0,TAU);
      gc.fillStyle=hexA(tint(color,.9),.85);gc.fill();
    });

    if(expanded){
      /* each ring's name set on its own arc, riding its largest bead gap */
      for(const rg of s.rings){
        const th=rg.anchor0+rg.dir*t*DRIFT;
        const flip=Math.sin(th)>0;   /* keep bottom-half labels upright */
        arcText(gc,rg.label,cx,cy,rg.R+(flip?11:3),th,
          `400 8.5px ${MONO}`,INK3,.95,flip);
      }

      text(gc,String(s.beads.length),cx,cy+36,{font:`500 24px ${SERIF}`,col:INK});
      text(gc,'SESSION ARCHIVES',cx,cy+52,
        {font:`400 8px ${MONO}`,col:INK3,track:.16});

      /* bead labels: two freshest per ring, middle-ellipsised; greedy
         vertical stagger + leader line whenever two land within LINE_H */
      const labs=s.notable.map(b=>{
        const nm=midTrunc(b.name);
        const th=Math.atan2(b.sy-cy,b.sx-cx);
        const off=b.r*2.4+7;
        const lx=b.sx+Math.cos(th)*off;
        const c=Math.cos(th);
        const align=c>.35?'left':c<-.35?'right':'center';
        const vy=align==='center'?(Math.sin(th)>0?9:-5):3;
        const ly=Math.max(12,Math.min(H-8,b.sy+Math.sin(th)*off+vy));
        const w=nm.length*5.5;
        const x0=align==='left'?lx:align==='right'?lx-w:lx-w/2;
        return {b,nm,th,lx,ly,ly0:ly,align,x0,x1:x0+w,pushed:false};
      });
      labs.sort((a,b)=>a.ly-b.ly);
      for(let pass=0;pass<2;pass++)
        for(let i=1;i<labs.length;i++)for(let j=0;j<i;j++){
          const p=labs[j],q=labs[i];
          if(q.ly-p.ly<LINE_H&&q.ly-p.ly>-LINE_H
            &&q.x0<p.x1+8&&p.x0<q.x1+8){q.ly=p.ly+LINE_H;q.pushed=true;}
        }
      for(const l of labs){
        if(l.pushed||Math.abs(l.ly-l.ly0)>1){
          const tx=l.align==='left'?l.lx-3:l.align==='right'?l.lx+3:l.lx;
          gc.strokeStyle=hexA(color,.35);gc.lineWidth=1;
          gc.beginPath();
          gc.moveTo(l.b.sx+Math.cos(l.th)*(l.b.r+2),
            l.b.sy+Math.sin(l.th)*(l.b.r+2));
          gc.lineTo(tx,l.ly-3);gc.stroke();
        }
        text(gc,l.nm,l.lx,l.ly,
          {font:`400 9px ${MONO}`,col:INK2,align:l.align,alpha:.9});
      }
      text(gc,'LABELS · 2 FRESHEST PER RING · HOVER ANY BEAD FOR ALL',
        14,H-10,{font:`400 7.5px ${MONO}`,col:INK3,align:'left',
          track:.12,alpha:.8});
    }
  },

  /* live hit points: getters read the drifting positions draw() records */
  hits(s,env){
    return s.beads.map(b=>({
      get x(){return b.sx;},get y(){return b.sy;},
      r:Math.max(11,b.r*2.2),
      tip:{kick:'session archive · '+b.ring.label,title:b.name,
        rows:[['Records',String(b.count)],['Last touched',fmtDate(b.date)]]}}));
  }
};
