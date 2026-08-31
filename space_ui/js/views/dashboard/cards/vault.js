/* q1 · Security & Setup — "Ember Vault".
   A lock sigil (concentric soft rings around a slowly turning diamond heart)
   guarded by three orbit bands of files: SECRET files closest as hot embers
   with stroked-diamond cores, SETUP files calmer on the middle band, CONFIG
   as faint dust on the outer band. Every ember is one real file — angle and
   radius jitter seeded by fnv(path), brightness by freshness(date). Between
   the sigil and the file orbits, one faint drifting mote per PROJECT bridges
   the mid band. Duplicate basenames are labelled with their parent directory.
   Bands revolve very slowly in alternating directions (~90s+). Guide rings
   and the sigil are baked into a static base layer; only embers, motes, one
   scan spark and the breathing heart animate. Names only — contents are
   never read. */
import {TAU,INK2,INK3,MONO,hexA,tint,fnv,freshness,glowSprite,drawGlow,
  withAdditive,ember,softRing,text,layer,fmtDate}from'../lib.js';

/* band geometry + orbital motion: radius as a fraction of min(W,H),
   seconds per revolution, direction (alternating) */
const BANDS={
  secret:{rf:.205,period:92,dir:1},
  setup:{rf:.29,period:108,dir:-1},
  config:{rf:.375,period:126,dir:1}
};
const KINDS=['secret','setup','config'];

export default{
  kind:'vault',

  init(data,env){
    const {W,H,dpr,color,expanded}=env;
    const u=Math.min(W,H);
    const cx=W/2,cy=H/2-(expanded?H*.012:0);
    const sprite=glowSprite(color);

    /* flatten projects[].tiles[] into one file list */
    const files=[];
    for(const p of (data&&data.projects)||[])
      for(const f of p.tiles||[])files.push({...f,project:p.name});
    const byKind={secret:[],setup:[],config:[]};
    for(const f of files)(byKind[f.kind]||byKind.config).push(f);
    const counts={secret:byKind.secret.length,setup:byKind.setup.length,
      config:byKind.config.length};

    /* duplicate basenames get a parent-directory prefix (api/install.sh) */
    const nameCount={};
    for(const f of files)nameCount[f.name]=(nameCount[f.name]||0)+1;
    const labelOf=f=>{
      if(nameCount[f.name]<2)return f.name;
      const seg=f.path.split('/');
      return (seg.length>1?seg[seg.length-2]+'/':'')+f.name;
    };

    /* one orb per file: even spacing per band + fnv(path) angle/radius jitter */
    const orbs=[];
    for(const kind of KINDS){
      const band=BANDS[kind],arr=byKind[kind],R=u*band.rf;
      arr.forEach((f,i)=>{
        const a0=-Math.PI/2+i*TAU/arr.length
          +(fnv(f.path)-.5)*(TAU/Math.max(3,arr.length))*.55;
        orbs.push({f,kind,band,a0,label:labelOf(f),
          r:R+(fnv(f.path+'~r')-.5)*u*.034,
          fresh:freshness(f.date),
          phase:fnv(f.path+'~p')*TAU});
      });
    }

    /* one faint mote per project drifting in the mid band between the sigil
       ring stack and the secret orbit — each maps to a real project */
    const motes=((data&&data.projects)||[]).map(p=>({
      name:p.name,n:(p.tiles||[]).length,
      nSecret:(p.tiles||[]).filter(f=>f.kind==='secret').length,
      a0:fnv('mote~'+p.name)*TAU,
      r:u*(.162+(fnv(p.name+'~mr')-.5)*.024),
      period:140,dir:-1,phase:fnv(p.name+'~mp')*TAU}));

    /* static base: band guide rings (with bloom on the ring nearest the
       core) + the lock sigil's ring stack */
    const base=layer(W,H,dpr);
    const g=base.g;
    withAdditive(g,()=>{
      /* wide faint halo strokes give the orbit rings a touch of bloom */
      g.strokeStyle=hexA(color,.06);g.lineWidth=10;
      g.beginPath();g.arc(cx,cy,u*BANDS.secret.rf,0,TAU);g.stroke();
      g.strokeStyle=hexA(color,.035);g.lineWidth=8;
      g.beginPath();g.arc(cx,cy,u*BANDS.setup.rf,0,TAU);g.stroke();
      softRing(g,cx,cy,u*BANDS.secret.rf,color,1,.17);
      softRing(g,cx,cy,u*BANDS.setup.rf,color,1,.12);
      softRing(g,cx,cy,u*BANDS.config.rf,color,1,.085);
      if(motes.length)softRing(g,cx,cy,u*.162,color,1,.06); /* project band */
      softRing(g,cx,cy,u*.128,color,1,.32);
      softRing(g,cx,cy,u*.096,color,1.2,.55);
      softRing(g,cx,cy,u*.066,color,1,.4);
      drawGlow(g,sprite,cx,cy,u*.145,.22);
    });

    return {orbs,motes,counts,base,sprite,cx,cy,u,lastT:null,legend:null,
      rSecret:u*BANDS.secret.rf};
  },

  draw(gc,s,env,t,mouse){
    const {W,H,color,expanded,reduced}=env;
    s.lastT=t;
    const {cx,cy,u,sprite}=s;
    s.base.blit(gc);

    withAdditive(gc,()=>{
      for(const o of s.orbs){
        const a=o.a0+(reduced?0:o.band.dir*t*TAU/o.band.period);
        const x=cx+Math.cos(a)*o.r,y=cy+Math.sin(a)*o.r;
        o.a=a;o.x=x;o.y=y;      /* cached for labels + hits */
        const pulse=reduced?.9:.86+.14*Math.sin(t*TAU/6.5+o.phase);
        if(o.kind==='secret'){  /* danger reads as heat */
          const rr=2.6+1.7*o.fresh,al=(.66+.3*o.fresh)*pulse;
          drawGlow(gc,sprite,x,y,rr*6,al*.32);  /* wide heat halo */
          ember(gc,sprite,color,x,y,rr,al,.86);
          const d=rr+2.8;       /* tiny stroked-diamond core */
          gc.beginPath();
          gc.moveTo(x,y-d);gc.lineTo(x+d,y);gc.lineTo(x,y+d);gc.lineTo(x-d,y);
          gc.closePath();
          gc.strokeStyle=hexA(tint(color,.85),Math.min(1,al*.95));
          gc.lineWidth=1;gc.stroke();
        }else if(o.kind==='setup'){ /* calmer ember discs */
          ember(gc,sprite,color,x,y,2.1+1.1*o.fresh,(.5+.32*o.fresh)*pulse,.7);
        }else{                       /* config dust — same glow floor */
          drawGlow(gc,sprite,x,y,6+2.5*o.fresh,(.34+.3*o.fresh)*pulse);
          gc.beginPath();gc.arc(x,y,1.2,0,TAU);
          gc.fillStyle=hexA(tint(color,.6),.42+.3*o.fresh);gc.fill();
        }
      }
      /* project motes drifting through the mid band */
      for(const m of s.motes){
        const a=m.a0+(reduced?0:m.dir*t*TAU/m.period);
        const x=cx+Math.cos(a)*m.r,y=cy+Math.sin(a)*m.r;
        m.x=x;m.y=y;
        const flicker=reduced?.85:.75+.25*Math.sin(t*TAU/11+m.phase);
        drawGlow(gc,sprite,x,y,5.5,.34*flicker);
        gc.beginPath();gc.arc(x,y,1,0,TAU);
        gc.fillStyle=hexA(tint(color,.55),.42*flicker);gc.fill();
      }
      /* one travelling scan spark patrolling the secret band */
      if(!reduced&&s.counts.secret){
        const sa=-Math.PI/2+t*TAU/24;
        drawGlow(gc,sprite,cx+Math.cos(sa)*s.rSecret,
          cy+Math.sin(sa)*s.rSecret,6,.38);
      }
      /* sigil heart: slowly turning diamond over a breathing white-hot core */
      const breath=reduced?.5:.5+.5*Math.sin(t*TAU/7);
      const d=u*.030;
      gc.save();
      gc.translate(cx,cy);gc.rotate(reduced?Math.PI/4:t*TAU/64);
      gc.beginPath();
      gc.moveTo(0,-d);gc.lineTo(d,0);gc.lineTo(0,d);gc.lineTo(-d,0);
      gc.closePath();
      gc.strokeStyle=hexA(tint(color,.75),.5+.3*breath);
      gc.lineWidth=1.2;gc.stroke();
      gc.restore();
      drawGlow(gc,sprite,cx,cy,u*.06+breath*u*.014,.6);
      drawGlow(gc,sprite,cx,cy,u*.026,.85);
      gc.beginPath();gc.arc(cx,cy,2.2+breath*.8,0,TAU);
      gc.fillStyle=hexA(tint(color,.92),.95);gc.fill();
    });

    if(expanded){
      /* one ink level for every file label — no unencoded dimness */
      for(const o of s.orbs){
        const ca=Math.cos(o.a),sa=Math.sin(o.a);
        const lr=o.r+12;
        const lx=cx+ca*lr,ly=cy+sa*lr;
        const opts={font:`400 9px ${MONO}`,col:INK2,alpha:.85};
        if(ca>.32)text(gc,o.label,o.x+10,ly+3,{...opts,align:'left'});
        else if(ca<-.32)text(gc,o.label,o.x-10,ly+3,{...opts,align:'right'});
        else text(gc,o.label,lx,o.y+(sa>0?16:-9),opts);
      }
      drawLegend(gc,s,env);
      if(!s.orbs.length)
        text(gc,'NO SECRET OR SETUP FILES TRACKED',W/2,cy+u*.19,
          {font:`400 8.5px ${MONO}`,col:INK3,track:.16});
    }
  },

  hits(s,env){
    const tt=s.lastT==null?12:s.lastT;
    const out=s.orbs.map(o=>{
      const a=o.a0+(env.reduced?0:o.band.dir*tt*TAU/o.band.period);
      const cut=o.f.path.lastIndexOf('/');
      return {x:s.cx+Math.cos(a)*o.r,y:s.cy+Math.sin(a)*o.r,r:15,
        tip:{kick:o.kind+' · names only — contents never read',
          title:o.f.name,
          rows:[['Where',cut>0?o.f.path.slice(0,cut):o.f.project],
            ['Modified',fmtDate(o.f.date)]]}};
    });
    for(const m of s.motes){
      const a=m.a0+(env.reduced?0:m.dir*tt*TAU/m.period);
      out.push({x:s.cx+Math.cos(a)*m.r,y:s.cy+Math.sin(a)*m.r,r:12,
        tip:{kick:'project',title:m.name,
          rows:[['Files tracked',String(m.n)],
            ['Secret-like',String(m.nSecret)]]}});
    }
    return out;
  }
};

/* bottom legend keyed with the actual glyphs: stroked diamond = SECRET,
   ember disc = SETUP, dust dot = CONFIG */
function drawLegend(gc,s,env){
  const {W,H,color}=env;
  const {secret,setup,config}=s.counts;
  const font=`400 8.5px ${MONO}`;
  if(!s.legend){
    gc.save();gc.font=font;gc.letterSpacing='1.8px';
    const parts=[`${secret} SECRET`,`${setup} SETUP`,`${config} CONFIG`]
      .map(t=>({t,w:gc.measureText(t).width}));
    const sep=gc.measureText(' · ').width;
    gc.restore();
    const GL=13; /* glyph slot width */
    const total=parts.reduce((a,p)=>a+GL+p.w,0)+sep*2;
    s.legend={parts,sep,GL,total};
  }
  const {parts,sep,GL}=s.legend;
  const by=H-12,gy=by-3;   /* text baseline / glyph centre */
  let x=W/2-s.legend.total/2;
  const glyphs=[
    gx=>{const d=3.2;   /* diamond */
      gc.beginPath();
      gc.moveTo(gx,gy-d);gc.lineTo(gx+d,gy);gc.lineTo(gx,gy+d);gc.lineTo(gx-d,gy);
      gc.closePath();
      gc.strokeStyle=hexA(tint(color,.85),.8);gc.lineWidth=1;gc.stroke();},
    gx=>{               /* setup ember disc */
      drawGlow(gc,s.sprite,gx,gy,5,.5);
      gc.beginPath();gc.arc(gx,gy,1.9,0,TAU);
      gc.fillStyle=hexA(tint(color,.7),.85);gc.fill();},
    gx=>{               /* config dust dot */
      drawGlow(gc,s.sprite,gx,gy,4,.35);
      gc.beginPath();gc.arc(gx,gy,1.2,0,TAU);
      gc.fillStyle=hexA(tint(color,.6),.6);gc.fill();}
  ];
  parts.forEach((p,i)=>{
    withAdditive(gc,()=>glyphs[i](x+GL/2-1));
    text(gc,p.t,x+GL,by,{font,col:INK3,track:.18,align:'left'});
    x+=GL+p.w+(i<2?sep:0);
    if(i<2)text(gc,'·',x-sep/2,by,{font,col:INK3,align:'center'});
  });
}
