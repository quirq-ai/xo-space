/* q7 · Projects — "The Cluster".
   Five environment gravity wells sit on pentagon bearings; a well's pull
   toward the frame edge scales with how many projects it holds, so empty
   and small environments draw in close to the cluster instead of leaving
   dead sky. An empty environment is an honest dashed ghost ring (the same
   ghost stroke as q6's worktree orbit). Every project is a star placed by
   the pull of its memberships — its position IS its identity — sized by
   log(mapped files), brightened by freshness, tied to its wells by
   filaments that brighten where they land on the environment ring (a small
   contact glow per membership, so the per-environment counts can be read
   off the anchor points). Filaments, contact glows, anchor rings and ghost
   rings bake into a static base layer; only the nebulas breathe (a tight
   two-pass bloom, no fog), the stars twinkle as crisp embers, and the
   busiest environment carries the system's white-hot core. Expanded star
   labels run a radial collision pass — nudged outward from the cluster
   centroid, growing a faint leader line once they leave their star — and
   yield to well labels, rings and other stars. The whole system drifts
   ±1.5° on a 60s sine. Strictly monochrome: env.color + tints. */
import {TAU,INK,INK2,INK3,MONO,SERIF,SANS,hexA,tint,fnv,glowSprite,drawGlow,
  withAdditive,ember,softRing,text,layer,freshness,fmtDate}from'../lib.js';

const DRIFT=1.5*Math.PI/180;          /* ±1.5° system drift */
const DRIFT_PERIOD=60;                /* seconds per sine cycle */
const ENV_ORDER=['engineering','ops','documentation','research','marketing'];

/* membership filament: faint at the star, brightening into the well ring;
   returns the contact point on the ring so counts stay legible */
function filament(g,color,st,w){
  let dx=w.x-st.x,dy=w.y-st.y;
  const m=Math.hypot(dx,dy)||1;dx/=m;dy/=m;
  const x0=st.x+dx*(st.r*1.6),y0=st.y+dy*(st.r*1.6);
  const x1=w.x-dx*(w.ringR+1),y1=w.y-dy*(w.ringR+1);
  g.lineCap='round';
  g.strokeStyle=hexA(color,.06);g.lineWidth=3;
  g.beginPath();g.moveTo(x0,y0);g.lineTo(x1,y1);g.stroke();
  const grd=g.createLinearGradient(x0,y0,x1,y1);
  grd.addColorStop(0,hexA(tint(color,.45),.10));
  grd.addColorStop(.65,hexA(tint(color,.45),.14));
  grd.addColorStop(1,hexA(tint(color,.6),.44));
  g.strokeStyle=grd;g.lineWidth=1;
  g.beginPath();g.moveTo(x0,y0);g.lineTo(x1,y1);g.stroke();
  return [x1,y1];
}

export default{
  kind:'galaxy',

  init(data,env){
    const {W,H,dpr,color,expanded}=env;
    const u=Math.min(W,H),cx=W/2,cy=H/2;
    const Rx=Math.min(W*.38,u*.60),Ry=u*.32;   /* elliptical stage */
    const sc=Math.min(2.2,Math.max(.9,u/340)); /* nebula/ring scale */
    const ss=Math.min(1.6,Math.max(.9,u/380)); /* star scale */
    const sprite=glowSprite(color);

    /* --- gravity wells on pentagon bearings; radius earned by count --- */
    const envsIn=data.environments||[];
    const byId=Object.fromEntries(envsIn.map(e=>[e.id,e]));
    const order=ENV_ORDER.filter(id=>byId[id])
      .concat(envsIn.map(e=>e.id).filter(id=>!ENV_ORDER.includes(id)));
    const maxCount=Math.max(1,...envsIn.map(e=>e.count));
    const wells=order.map((id,i)=>{
      const e=byId[id];
      const ang=-Math.PI/2+i*TAU/Math.max(1,order.length);
      const pull=.52+.48*Math.sqrt(e.count/maxCount); /* empty pulls close */
      return {env:e,ang,
        x:cx+Math.cos(ang)*Rx*pull,y:cy+Math.sin(ang)*Ry*pull,
        nebR:(16+e.count*3)*sc,
        nebA:.16+.05*Math.min(e.count,6),
        ringR:9*sc,ghostR:16*sc,hot:false,
        phase:fnv('well:'+e.id)*TAU};
    });
    const busiest=wells.filter(w=>w.env.count===maxCount&&w.env.count>0)[0];
    if(busiest)busiest.hot=true;      /* the white-hot focal core */
    const wellById=Object.fromEntries(wells.map(w=>[w.env.id,w]));

    /* --- env labels are the map legend (positions static, drift in draw);
       computed before the stars so the relax pass can keep stars off them */
    const wl=wells.map(w=>{
      const sy=Math.sin(w.ang);
      const off=(w.env.count?w.ringR:w.ghostR)+(expanded?24:14)
        +Math.abs(Math.cos(w.ang))*(expanded?26:10);
      let ly=w.y+sy*off;
      ly+=sy>.5?(expanded?12:9):sy<-.5?(expanded?-10:-8):-2;
      return {w,x:w.x+Math.cos(w.ang)*off,y:ly,
        hw:w.env.label.length*(expanded?4.2:3.2)+6,
        yc:ly+(expanded?4:2),hh:expanded?19:13};
    });

    /* --- project stars: membership-weighted positions + seeded jitter --- */
    const jit=30*(expanded?1.9:1.1);
    const stars=(data.projects||[]).map(p=>{
      const anchors=(p.memberships||[]).map(id=>wellById[id]).filter(Boolean);
      let mx=cx,my=cy;
      if(anchors.length){
        mx=anchors.reduce((a,w)=>a+w.x,0)/anchors.length;
        my=anchors.reduce((a,w)=>a+w.y,0)/anchors.length;
      }
      const files=parseInt(p.blurb,10)||0;
      return {p,anchors,files,
        x:cx+(mx-cx)*.85+(fnv(p.id+':x')-.5)*2*jit,
        y:cy+(my-cy)*.85+(fnv(p.id+':y')-.5)*2*jit,
        r:(2+Math.log10(1+files)*1.5)*ss,
        fresh:freshness(p.date),
        phase:fnv(p.id)*TAU};
    });
    /* widen the cluster 1.6x about its own centroid so stars breathe */
    if(stars.length){
      const Cx=stars.reduce((a,s)=>a+s.x,0)/stars.length;
      const Cy=stars.reduce((a,s)=>a+s.y,0)/stars.length;
      for(const st of stars){st.x=Cx+(st.x-Cx)*1.6;st.y=Cy+(st.y-Cy)*1.6;}
    }
    /* relax: separate star pairs and keep stars clear of well rings */
    const minD=expanded?80:40;
    const clampStars=()=>{for(const st of stars){
      st.x=Math.min(W*.92,Math.max(W*.08,st.x));
      st.y=Math.min(H*.90,Math.max(H*.10,st.y));}};
    for(let it=0;it<50;it++){
      for(let i=0;i<stars.length;i++)for(let j=i+1;j<stars.length;j++){
        const a=stars[i],b=stars[j];
        let dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy);
        if(d>=minD)continue;
        if(d<.001){const th=fnv(a.p.id+b.p.id)*TAU;dx=Math.cos(th);dy=Math.sin(th);d=1;}
        const push=(minD-d)/2/d;
        a.x-=dx*push;a.y-=dy*push;b.x+=dx*push;b.y+=dy*push;
      }
      for(const st of stars)for(const w of wells){
        const keep=w.ringR+(expanded?34:20)+st.r;
        let dx=st.x-w.x,dy=st.y-w.y,d=Math.hypot(dx,dy);
        if(d>=keep)continue;
        if(d<.001){const th=fnv(st.p.id+w.env.id)*TAU;dx=Math.cos(th);dy=Math.sin(th);d=1;}
        st.x+=dx/d*(keep-d);st.y+=dy/d*(keep-d);
      }
      clampStars();
      for(const st of stars)for(const b of wl){ /* stars stay off the legend;
        an escape that would leave the frame goes sideways instead */
        const pad=st.r*2.4+5;
        const ox=b.hw+pad-Math.abs(st.x-b.x),oy=b.hh+pad-Math.abs(st.y-b.yc);
        if(ox<=0||oy<=0)continue;
        const yOk=st.y<b.yc?b.yc-(b.hh+pad)>H*.10:b.yc+(b.hh+pad)<H*.90;
        if(oy<=ox&&yOk)st.y+=st.y<b.yc?-oy:oy;
        else st.x+=st.x<b.x?-ox:ox;
      }
    }
    clampStars();

    /* --- static base: filaments, contact glows, anchor + ghost rings --- */
    const base=layer(W,H,dpr);
    const g=base.g;
    withAdditive(g,()=>{
      for(const st of stars)for(const w of st.anchors){
        const [tx,ty]=filament(g,color,st,w);
        drawGlow(g,sprite,tx,ty,3.4,.30);   /* one countable landing per edge */
      }
      for(const w of wells){
        if(w.env.count){softRing(g,w.x,w.y,w.ringR,color,1,.38);continue;}
        g.setLineDash([3,8]);               /* the honest zero — q6 ghost stroke */
        g.strokeStyle=hexA(tint(color,.4),expanded?.34:.30);
        g.lineWidth=1;
        g.beginPath();g.arc(w.x,w.y,w.ghostR,0,TAU);g.stroke();
        g.setLineDash([]);
        softRing(g,w.x,w.y,w.ringR*.5,color,1,.12);
      }
    });

    /* --- expanded star labels: radial collision pass from the centroid --- */
    let labels=null;
    if(expanded&&stars.length){
      const Cx=stars.reduce((a,s)=>a+s.x,0)/stars.length;
      const Cy=stars.reduce((a,s)=>a+s.y,0)/stars.length;
      g.font=`400 10px ${SANS}`;
      labels=stars.map(st=>{
        let dx=st.x-Cx,dy=st.y-Cy,m=Math.hypot(dx,dy);
        if(m<.001){const th=fnv(st.p.id+':a')*TAU;dx=Math.cos(th);dy=Math.sin(th);m=1;}
        return {st,ux:dx/m,uy:dy/m,rad:m,
          w:g.measureText(st.p.label).width,
          d:Math.max(st.r*3.2+9,15)};
      });
      const place=L=>{L.x=L.st.x+L.ux*L.d;L.y=L.st.y+L.uy*L.d+3.5;};
      labels.forEach(place);
      for(let it=0;it<90;it++){
        let moved=false;
        const nudge=L=>{L.d+=6;place(L);moved=true;};
        for(let i=0;i<labels.length;i++)for(let j=i+1;j<labels.length;j++){
          const A=labels[i],B=labels[j];
          if(Math.abs(A.x-B.x)<(A.w+B.w)/2+8&&Math.abs(A.y-B.y)<13)
            nudge(A.rad>=B.rad?A:B);        /* the outer label yields outward */
        }
        for(const L of labels){
          for(const st of stars){           /* clear of other stars' glow */
            if(st===L.st)continue;
            if(Math.abs(st.x-L.x)<L.w/2+8&&Math.abs(st.y-(L.y-3.5))<st.r*2.4+9)
              {nudge(L);break;}
          }
          for(const w of wells){            /* clear of rings + ghost rings */
            const rr=(w.env.count?w.ringR:w.ghostR)+12;
            if(Math.abs(w.x-L.x)<L.w/2+rr&&Math.abs(w.y-(L.y-3.5))<rr+7)
              {nudge(L);break;}
          }
          for(const b of wl)                /* clear of the env legend */
            if(Math.abs(b.x-L.x)<L.w/2+b.hw&&Math.abs(b.yc-L.y)<b.hh+7)
              {nudge(L);break;}
        }
        if(!moved)break;
      }
      for(const L of labels){               /* never clipped; leader if far */
        L.x=Math.min(Math.max(L.x,14+L.w/2),W-14-L.w/2);
        L.y=Math.min(Math.max(L.y,22),H-14);
        const gx=L.x-L.st.x,gy=(L.y-3.5)-L.st.y,gm=Math.hypot(gx,gy)||1;
        L.leader=gm>L.st.r*3.2+16;
        L.ax=L.st.x+gx/gm*(L.st.r*2.2);L.ay=L.st.y+gy/gm*(L.st.r*2.2);
        L.bx=L.st.x+gx/gm*(gm-9);L.by=L.st.y+gy/gm*(gm-9);
      }
    }

    return {wells,stars,wl,labels,base,sprite,cx,cy,rot:0,px:0,py:0};
  },

  draw(gc,s,env,t,mouse){
    const {W,color,expanded,reduced}=env;
    const rot=reduced?0:Math.sin(t*TAU/DRIFT_PERIOD)*DRIFT;
    const px=mouse?(mouse.x-s.cx)/W*6:0,py=mouse?(mouse.y-s.cy)/W*6:0;
    s.rot=rot;s.px=px;s.py=py;

    gc.save();
    gc.translate(s.cx+px,s.cy+py);gc.rotate(rot);gc.translate(-s.cx,-s.cy);
    withAdditive(gc,()=>{           /* tight two-pass nebulas, count-scaled */
      for(const w of s.wells){
        if(!w.env.count)continue;
        const br=reduced?0:Math.sin(t*TAU/26+w.phase);
        drawGlow(gc,s.sprite,w.x,w.y,w.nebR*(1+.04*br),w.nebA*(1+.10*br));
        drawGlow(gc,s.sprite,w.x,w.y,w.nebR*.55,w.nebA*.9);
      }
    });
    s.base.blit(gc);                /* filaments, contacts, rings, ghosts */
    withAdditive(gc,()=>{           /* crisp twinkling embers per project */
      for(const st of s.stars){
        const tw=reduced?1:.85+.15*Math.sin(t*1.25+st.phase);
        ember(gc,s.sprite,color,st.x,st.y,st.r,(.35+.65*st.fresh)*tw,.85);
      }
      for(const w of s.wells){      /* the busiest environment runs hot */
        if(!w.hot)continue;
        const breath=reduced?.5:.5+.5*Math.sin(t*TAU/5.2+w.phase);
        drawGlow(gc,s.sprite,w.x,w.y,w.ringR*1.7+breath*3,.85);
        drawGlow(gc,s.sprite,w.x,w.y,w.ringR*.8,.95);
        gc.beginPath();gc.arc(w.x,w.y,2.6+breath*.8,0,TAU);
        gc.fillStyle=hexA(tint(color,.92),.95);gc.fill();
      }
    });
    gc.restore();

    /* upright labels at drifted positions */
    const co=Math.cos(rot),sn=Math.sin(rot);
    const rp=(x,y)=>{
      const dx=x-s.cx,dy=y-s.cy;
      return [s.cx+dx*co-dy*sn+px,s.cy+dx*sn+dy*co+py];
    };
    for(const L of s.wl){
      const [lx,ly]=rp(L.x,L.y);
      const dim=L.w.env.count?1:.62;
      text(gc,L.w.env.label,lx,ly,
        {font:`500 ${expanded?15:11}px ${SERIF}`,col:INK2,alpha:(expanded?.85:.75)*dim});
      text(gc,expanded?L.w.env.count+(L.w.env.count===1?' project':' projects')
        :String(L.w.env.count),lx,ly+(expanded?16:10),
        {font:`400 ${expanded?9.5:7.5}px ${MONO}`,col:INK3,alpha:.9*dim});
    }
    if(expanded&&s.labels)for(const L of s.labels){
      if(L.leader){
        const [ax,ay]=rp(L.ax,L.ay),[bx,by]=rp(L.bx,L.by);
        gc.strokeStyle=hexA(tint(color,.4),.30);gc.lineWidth=.8;
        gc.beginPath();gc.moveTo(ax,ay);gc.lineTo(bx,by);gc.stroke();
      }
      const [lx,ly]=rp(L.x,L.y);
      text(gc,L.st.p.label,lx,ly,{font:`400 10px ${SANS}`,col:INK,alpha:.92});
    }
  },

  hits(s){
    const co=Math.cos(s.rot||0),sn=Math.sin(s.rot||0);
    const rp=(x,y)=>{
      const dx=x-s.cx,dy=y-s.cy;
      return [s.cx+dx*co-dy*sn+(s.px||0),s.cy+dx*sn+dy*co+(s.py||0)];
    };
    const out=[];
    for(const st of s.stars){
      const [x,y]=rp(st.x,st.y);
      out.push({x,y,r:Math.max(14,st.r*3),
        tip:{kick:'project · '+st.p.tag,title:st.p.label,sub:st.p.blurb,
          rows:[['Environments',st.anchors.map(w=>w.env.label).join(' + ')],
            ['Last mapped',fmtDate(st.p.date)]],
          foot:'Open on the Files graph from the Files tab'}});
    }
    for(const w of s.wells){
      const [x,y]=rp(w.x,w.y);
      out.push({x,y,r:(w.env.count?w.ringR:w.ghostR)+6,
        tip:{kick:'environment',title:w.env.label,
          sub:w.env.count?null:'empty — no projects assigned',
          rows:[['Projects',String(w.env.count)]]}});
    }
    return out;
  }
};
