/* q3 · Tools & Logs — "Pulsar".
   Every tool the agents called is a comet ray: length = log(calls), a
   white-hot ember at the tip (pulsing on its own phase), particle dust
   along the shaft. The burst owns the whole frame, vertically centered;
   in expanded mode the five system logs ride an outer orbit ring as
   luminous canisters (length = log(bytes), brightness = freshness), so
   the card stays one centerpiece. Grid mode is the bare burst — the
   caption bar already says "5 log files". The burst rotates imperceptibly
   in the grid; expanded mode freezes rotation so labels and hit targets
   hold still. Ray shafts, dust, the orbit ring and canister bodies are
   baked into a static base layer at init — only the core, tip embers,
   canister tip glows and travelling sparks animate per frame.
   Duplicate tool names (same tool from two runtimes) are suffixed with
   their runtime so two 'Read' rays read as intent, not a bug. Expanded
   labels run a radial collision pass at init: anchors sit past their tip,
   adjacent rays alternate radii, near-vertical sectors get extra offset
   (rays converge there), then overlapping pairs push the shorter ray's
   label further outward until the field is clean. */
import {TAU,INK,INK2,INK3,MONO,SERIF,hexA,tint,fnv,freshness,glowSprite,
  drawGlow,withAdditive,ember,softLine,text,layer,fmtDate}from'../lib.js';

const ROT_PERIOD=240;                /* grid: seconds per full rotation */
const RING_GAP=TAU*24/360;           /* half-arc kept clear for the ring caption */

const fmtBytes=b=>b>=1048576?(b/1048576).toFixed(1)+' MB'
  :b>=1024?Math.round(b/1024)+' KB':b+' B';
const truncMid=n=>n.length>16?n.slice(0,8)+'…'+n.slice(-6):n;

/* soft luminous arc — softRing with a caption gap at 12 o'clock */
function softArc(g,x,y,r,a0,a1,color,w,a){
  g.lineCap='round';
  g.strokeStyle=hexA(color,a*.22);g.lineWidth=w*3;
  g.beginPath();g.arc(x,y,r,a0,a1);g.stroke();
  g.strokeStyle=hexA(tint(color,.35),a);g.lineWidth=w;
  g.beginPath();g.arc(x,y,r,a0,a1);g.stroke();
}

export default{
  kind:'pulsar',

  init(data,env){
    const {W,H,dpr,color,expanded}=env;
    const tools=(data.tools||[]).slice(0,expanded?22:16);
    const logs=(data.logs||[]).slice(0,6);
    const maxCalls=Math.max(1,...tools.map(t=>t.calls));
    const cx=W/2,cy=H/2;
    const half=Math.min(W,H)/2;
    const ringR=half*.84;
    const maxLen=expanded?ringR*.76:half*.72;
    const sprite=glowSprite(color);

    /* a tool name appearing under two runtimes keeps both rays, labeled */
    const seen={};
    for(const t of tools)seen[t.name]=(seen[t.name]||0)+1;

    const rays=tools.map((tool,i)=>{
      const angle=-Math.PI/2+i*TAU/Math.max(1,tools.length);
      const len=maxLen*(.28+.72*Math.log2(1+tool.calls)/Math.log2(1+maxCalls));
      return {tool,angle,len,phase:fnv(tool.agent+tool.name)*TAU,
        label:seen[tool.name]>1?tool.name+' · '+tool.agent.split('_')[0]:tool.name,
        errFrac:tool.calls?tool.errors/tool.calls:0};
    });

    /* static base: shafts + dust around (cx,cy); grid draw() rotates the layer */
    const base=layer(W,H,dpr);
    const g=base.g;
    withAdditive(g,()=>{
      for(const ray of rays){
        const dx=Math.cos(ray.angle),dy=Math.sin(ray.angle);
        softLine(g,cx+dx*16,cy+dy*16,cx+dx*ray.len,cy+dy*ray.len,color,1,.3);
        const dots=Math.max(3,Math.floor(ray.len/9));
        for(let i=0;i<dots;i++){
          const f=i/(dots-1);
          const d=16+(ray.len-16)*f;
          const jitter=(fnv(ray.tool.name+i)-.5)*3.5;
          drawGlow(g,sprite,cx+dx*d-dy*jitter,cy+dy*d+dx*jitter,
            2.6+f*3.2,.10+f*.28);
        }
      }
    });

    /* system logs — canisters on an outer orbit (expanded only); angle is
       deterministic placement, length = log(bytes), brightness = freshness */
    const maxSize=Math.max(1,...logs.map(l=>l.size));
    const arc=TAU-RING_GAP*2;
    const caps=logs.map((log,i)=>({log,
      angle:-Math.PI/2+RING_GAP+(i+.5)*arc/Math.max(1,logs.length)
        +(fnv(log.name)-.5)*.1,
      h:10+16*Math.log2(1+log.size)/Math.log2(1+maxSize),
      fresh:freshness(log.date),phase:fnv(log.name)*TAU}));
    if(expanded&&caps.length)withAdditive(g,()=>{
      softArc(g,cx,cy,ringR,-Math.PI/2+RING_GAP,TAU-Math.PI/2-RING_GAP,color,1,.28);
      for(const c of caps){
        const px=cx+Math.cos(c.angle)*ringR,py=cy+Math.sin(c.angle)*ringR;
        g.save();g.translate(px,py);g.rotate(c.angle);
        const grd=g.createLinearGradient(-c.h/2,0,c.h/2,0);
        grd.addColorStop(0,hexA(color,.12));
        grd.addColorStop(1,hexA(tint(color,.55),.35+.55*c.fresh));
        g.fillStyle=grd;
        g.beginPath();g.roundRect(-c.h/2,-2.2,c.h,4.4,2.2);g.fill();
        g.restore();
      }
    });

    /* expanded ray labels: anchor past the tip, then a radial collision pass */
    let labels=null;
    if(expanded&&rays.length){
      g.font=`400 9.5px ${MONO}`;
      const minR=maxLen*.6,maxR=ringR-26;
      labels=rays.map((ray,i)=>{
        const sy=Math.sin(ray.angle);
        return {ray,w:g.measureText(ray.label).width,
          lr:Math.max(ray.len+22,minR)+(i%2)*13+sy*sy*(sy>0?34:22)};
      });
      const place=L=>{
        const ux=Math.cos(L.ray.angle),uy=Math.sin(L.ray.angle);
        const px=cx+ux*L.lr,py=cy+uy*L.lr;
        L.align=ux>.35?'left':ux<-.35?'right':'center';
        L.x=L.align==='left'?px+2:L.align==='right'?px-2:px;
        L.y=py+uy*7+3.5;
        L.x0=L.align==='left'?L.x:L.align==='right'?L.x-L.w:L.x-L.w/2;
        L.x1=L.x0+L.w;
      };
      labels.forEach(place);
      for(let it=0;it<90;it++){
        let moved=false;
        for(let i=0;i<labels.length;i++)for(let j=i+1;j<labels.length;j++){
          const A=labels[i],B=labels[j];
          if(A.x0<B.x1+6&&B.x0<A.x1+6&&Math.abs(A.y-B.y)<12){
            if(A.lr>=maxR&&B.lr>=maxR)continue;
            let L=A.ray.len<B.ray.len?A:B;   /* the shorter ray's label yields */
            if(L.lr>=maxR)L=L===A?B:A;
            L.lr+=7;place(L);moved=true;
          }
        }
        if(!moved)break;
      }
      for(const L of labels){                /* never clipped at the frame */
        const lo=L.align==='left'?0:L.align==='center'?L.w/2:L.w;
        const hi=L.align==='right'?0:L.align==='center'?L.w/2:L.w;
        L.x=Math.min(Math.max(L.x,12+lo),W-12-hi);
        L.y=Math.min(Math.max(L.y,18),H-10);
      }
    }

    /* log labels sit just outside the orbit, aligned away from the ring */
    const logLabels=expanded?caps.map(c=>{
      const ux=Math.cos(c.angle),uy=Math.sin(c.angle);
      const px=cx+ux*(ringR+16),py=cy+uy*(ringR+16);
      return {c,align:ux>.35?'left':ux<-.35?'right':'center',
        x:px,y:Math.min(Math.max(py+uy*8,22),H-18)};
    }):null;

    return {rays,caps,labels,logLabels,base,cx,cy,ringR,sprite,rot:0,
      totalCalls:(data.tools||[]).reduce((s,t)=>s+t.calls,0),
      totalErrs:(data.tools||[]).reduce((s,t)=>s+t.errors,0),
      totalBytes:logs.reduce((s,l)=>s+l.size,0)};
  },

  draw(gc,s,env,t,mouse){
    const {W,H,color,expanded,reduced}=env;
    const rot=(reduced||expanded)?0:(t/ROT_PERIOD)*TAU;
    s.rot=rot;
    const px=mouse?(mouse.x-W/2)/W*8:0,py=mouse?(mouse.y-H/2)/H*8:0;

    gc.save();
    gc.translate(s.cx+px,s.cy+py);
    gc.rotate(rot);
    gc.translate(-s.cx,-s.cy);
    s.base.blit(gc);
    gc.restore();
    const cx=s.cx+px,cy=s.cy+py;

    withAdditive(gc,()=>{
      for(const ray of s.rays){
        const a=ray.angle+rot;
        const x=cx+Math.cos(a)*ray.len,y=cy+Math.sin(a)*ray.len;
        const pulse=reduced?.75:.6+.4*Math.sin(t*1.6+ray.phase);
        ember(gc,s.sprite,color,x,y,1.9+pulse*1.1,.5+pulse*.5);
        if(ray.errFrac>0){ /* a dim halo ring marks a ray that errors */
          gc.beginPath();gc.arc(x,y,6.5,0,TAU);
          gc.strokeStyle=hexA(tint(color,.7),.14+.3*ray.errFrac);
          gc.lineWidth=1;gc.stroke();
        }
      }
      /* travelling sparks on a rotating subset of rays */
      if(!reduced)for(let k=0;k<4;k++){
        const ray=s.rays[(Math.floor(t/5)+k*3)%Math.max(1,s.rays.length)];
        if(!ray)continue;
        const f=(t%5)/5;
        const a=ray.angle+rot;
        const d=16+(ray.len-16)*f;
        drawGlow(gc,s.sprite,cx+Math.cos(a)*d,cy+Math.sin(a)*d,5,(1-f)*.65);
      }
      /* breathing core */
      const breath=reduced?.5:.5+.5*Math.sin(t*TAU/4.2);
      drawGlow(gc,s.sprite,cx,cy,34+breath*8,.75);
      drawGlow(gc,s.sprite,cx,cy,15,.95);
      gc.beginPath();gc.arc(cx,cy,3.4+breath*.8,0,TAU);
      gc.fillStyle=hexA(tint(color,.92),.95);gc.fill();
      /* canister tip glows on the log orbit */
      if(expanded)for(const c of s.caps){
        const pulse=reduced?.6:.55+.35*Math.sin(t*.8+c.phase);
        drawGlow(gc,s.sprite,cx+Math.cos(c.angle)*(s.ringR+c.h/2),
          cy+Math.sin(c.angle)*(s.ringR+c.h/2),6.5,(.25+.5*c.fresh)*pulse);
      }
    });

    if(expanded){
      /* soft scrim so lower shafts fade behind the hero figure */
      const ny=cy+54;
      const scr=gc.createRadialGradient(cx,ny,0,cx,ny,78);
      scr.addColorStop(0,'rgba(7,8,10,.78)');
      scr.addColorStop(1,'rgba(7,8,10,0)');
      gc.save();gc.translate(cx,ny);gc.scale(1,.42);gc.translate(-cx,-ny);
      gc.fillStyle=scr;gc.beginPath();gc.arc(cx,ny,78,0,TAU);gc.fill();
      gc.restore();
      text(gc,String(s.totalCalls),cx,cy+58,{font:`500 26px ${SERIF}`,col:INK});
      text(gc,'TOOL CALLS',cx,cy+74,{font:`400 8px ${MONO}`,col:INK3,track:.16});
      if(s.labels)for(const L of s.labels)
        text(gc,L.ray.label,L.x+px,L.y+py,
          {font:`400 9.5px ${MONO}`,col:INK2,align:L.align,alpha:.85});
      if(s.caps.length){
        text(gc,'SYSTEM LOGS · '+fmtBytes(s.totalBytes),cx,cy-s.ringR+3.5,
          {font:`400 8px ${MONO}`,col:INK3,track:.16});
        for(const L of s.logLabels){
          text(gc,truncMid(L.c.log.name),L.x+px,L.y+py,
            {font:`400 9px ${MONO}`,col:INK2,align:L.align,alpha:.9});
          text(gc,L.c.log.sizeLabel,L.x+px,L.y+py+11,
            {font:`400 8px ${MONO}`,col:INK3,align:L.align});
        }
      }
    }
  },

  hits(s,env){
    const out=[{x:s.cx,y:s.cy,r:26,
      tip:{kick:'q3 · tools',title:s.totalCalls+' tool calls',
        sub:s.rays.length+' tools across runtimes',
        rows:[['Errors',String(s.totalErrs)]]}}];
    for(const ray of s.rays){
      const a=ray.angle+(s.rot||0);
      out.push({x:s.cx+Math.cos(a)*ray.len,y:s.cy+Math.sin(a)*ray.len,r:14,
        tip:{kick:'tool · '+ray.tool.agent,title:ray.tool.name,
          rows:[['Calls',String(ray.tool.calls)],
            ['Errors',String(ray.tool.errors)],
            ['Last used',fmtDate(ray.tool.day)]]}});
    }
    for(const c of s.caps){
      out.push({x:s.cx+Math.cos(c.angle)*s.ringR,
        y:s.cy+Math.sin(c.angle)*s.ringR,r:16,
        tip:{kick:'system log',title:c.log.name,
          rows:[['Size',c.log.sizeLabel],['Modified',fmtDate(c.log.date)]]}});
    }
    return out;
  }
};
