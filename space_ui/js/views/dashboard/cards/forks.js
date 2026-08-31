/* q6 · Agent Workspaces — "Nexus".
   One centerpiece: a white-hot breathing hub (the agent workspace) with
   each git repository as a luminous spiral strand curling out of it —
   motes flow hub -> head, and the comet head (one bright bloom per repo)
   carries the repo label in expanded mode. Worktrees, when any exist,
   branch off their strand as short forks tipped with an ember; while
   there are none, a faint dashed ghost orbit hugs the hub, captioned so
   the zero reads intentional. The agent task workspaces ride the outer
   orbit as glowing satellites — size and brightness = recency
   (lib.freshness), newest at the upper right, ageing clockwise; expanded
   labels show age + short id, hover carries the full hash. Strand spines,
   arm dust and both rings are baked into a static layer at init; only the
   motes, comet heads, satellites, two patrolling orbit sparks and the
   breathing core animate. The whole figure rotates imperceptibly in the
   grid; expanded mode freezes rotation so labels and hit targets hold. */
import {TAU,INK,INK2,INK3,MONO,SERIF,hexA,tint,fnv,daysSince,freshness,
  glowSprite,drawGlow,withAdditive,ember,softRing,text,layer,fmtDate}from'../lib.js';

const FLOW=11;        /* seconds for a mote to travel hub -> head */
const MOTES=24;       /* animated motes per strand */
const SWEEP=2.2;      /* radians each strand curls around the hub */
const ROT_PERIOD=260; /* grid: seconds per full rotation */
const WT_GAP=.48;     /* ghost-ring caption gap (radians, half-arc) */

const fmtAge=d=>{const n=daysSince(d);
  return n==null?'':n<1?'today':Math.round(n)+'d ago';};

/* strand offset from the hub at flow fraction f (0 = hub, 1 = head) */
function armPos(arm,f,rot=0){
  const r=arm.r0+(arm.r1-arm.r0)*Math.pow(f,.9);
  const th=arm.th0+SWEEP*f+Math.sin(f*TAU+arm.wp)*arm.wa+rot;
  return [Math.cos(th)*r,Math.sin(th)*r,th];
}

export default{
  kind:'forks',

  init(data,env){
    const {W,H,dpr,color,expanded}=env;
    const repos=data.repos||[];
    const tasks=(data.tasks||[]).slice(0,12);
    const sprite=glowSprite(color);
    const cx=W/2,cy=H/2,half=Math.min(W,H)/2;
    const Rorb=half*.80, Rwt=half*(expanded?.34:.40);

    /* each repo = one spiral strand; heads land near 12 and 6 o'clock so
       expanded labels sit above/below, clear of the orbit's diagonals */
    const arms=repos.map((repo,i)=>{
      const arm={repo,
        th0:-.43+i*TAU/Math.max(1,repos.length),
        r0:half*.09,
        r1:half*.60*(.92+.12*fnv(repo.name+'len')),
        wa:.05+.05*fnv(repo.name+'wa'),
        wp:fnv(repo.name+'wp')*TAU,
        per:FLOW*(.88+.28*fnv(repo.name+'per')),
        ph:fnv(repo.name+'ph')*TAU,
        motes:[],branches:[]};
      for(let k=0;k<MOTES;k++)arm.motes.push({
        ph:(k+fnv(repo.name+'m'+k))/MOTES,
        aa:.6+.4*fnv(repo.name+'b'+k)});
      const [hx,hy]=armPos(arm,1);
      arm.hx=hx;arm.hy=hy;
      (repo.worktrees||[]).forEach((wt,j)=>{
        const f0=.72-j*.12;
        const [sx,sy,th]=armPos(arm,f0);
        arm.branches.push({name:wt.name||String(wt),sx,sy,
          ex:sx+Math.cos(th+.9)*half*.17,ey:sy+Math.sin(th+.9)*half*.17,
          ph:fnv(repo.name+'wt'+j)*TAU});
      });
      return arm;
    });
    const nWt=arms.reduce((n,a)=>n+a.branches.length,0);

    /* task satellites on the outer orbit — newest upper-right, clockwise */
    const orbs=tasks.slice().sort((a,b)=>(b.date||'').localeCompare(a.date||''))
      .map((task,i)=>({task,
        ang:-Math.PI/2+(i+.5)*TAU/Math.max(1,tasks.length)
          +(fnv(task.name)-.5)*.07,
        fresh:freshness(task.date),ph:fnv(task.name)*TAU}));

    /* static base: strand spines + dust, worktree forks, both rings */
    const base=layer(W,H,dpr);
    const g=base.g;
    withAdditive(g,()=>{
      g.lineCap='round';g.lineJoin='round';
      for(const arm of arms){
        g.beginPath();
        for(let k=0;k<=48;k++){
          const [dx,dy]=armPos(arm,k/48);
          k?g.lineTo(cx+dx,cy+dy):g.moveTo(cx+dx,cy+dy);
        }
        g.strokeStyle=hexA(color,.06);g.lineWidth=5;g.stroke();
        g.strokeStyle=hexA(tint(color,.45),.24);g.lineWidth=1.2;g.stroke();
        /* dust broadening + brightening toward the comet head */
        const N=expanded?90:60;
        for(let k=0;k<N;k++){
          const f=(k+fnv(arm.repo.name+'d'+k))/N;
          const [dx,dy,th]=armPos(arm,f);
          const off=(fnv(arm.repo.name+'o'+k)-.5)*(3+9*f);
          drawGlow(g,sprite,cx+dx-Math.sin(th)*off,cy+dy+Math.cos(th)*off,
            (expanded?2.8:2)+(expanded?3.6:2.3)*f,.13+.33*f);
        }
        for(const b of arm.branches){
          g.beginPath();g.moveTo(cx+b.sx,cy+b.sy);
          g.quadraticCurveTo(cx+(b.sx+b.ex)/2,cy+(b.sy+b.ey)/2-4,
            cx+b.ex,cy+b.ey);
          g.strokeStyle=hexA(color,.08);g.lineWidth=3.4;g.stroke();
          g.strokeStyle=hexA(tint(color,.5),.34);g.lineWidth=1.1;g.stroke();
        }
      }
      /* the agent-task orbit */
      softRing(g,cx,cy,Rorb,color,1.1,.30);
      /* the worktree ghost orbit — the honest zero */
      if(!nWt){
        g.setLineDash([3,8]);
        g.strokeStyle=hexA(tint(color,.4),expanded?.34:.22);
        g.lineWidth=1;
        g.beginPath();
        if(expanded)g.arc(cx,cy,Rwt,-Math.PI/2+WT_GAP,TAU-Math.PI/2-WT_GAP);
        else g.arc(cx,cy,Rwt,0,TAU);
        g.stroke();
        g.setLineDash([]);
      }
    });

    /* expanded labels: satellites outside the orbit, repos past their head */
    const orbLabels=expanded?orbs.map(o=>{
      const ux=Math.cos(o.ang),uy=Math.sin(o.ang);
      const px=cx+ux*(Rorb+18),py=cy+uy*(Rorb+18);
      return {o,align:ux>.35?'left':ux<-.35?'right':'center',
        x:px,y:Math.min(Math.max(py+uy*8,24),H-24)};
    }):null;
    const headLabels=expanded?arms.map(arm=>{
      const m=Math.hypot(arm.hx,arm.hy)||1;
      const ux=arm.hx/m,uy=arm.hy/m;
      const px=cx+arm.hx+ux*24,py=cy+arm.hy+uy*24;
      return {arm,align:ux>.35?'left':ux<-.35?'right':'center',
        x:px,y:Math.min(Math.max(py+uy*8,26),H-26)};
    }):null;

    return {arms,orbs,orbLabels,headLabels,base,sprite,cx,cy,Rorb,Rwt,nWt,
      rot:0,moteR:expanded?2.4:1.7,headR:expanded?26:15,
      orbR:expanded?12:8,coreR:expanded?44:25};
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
      for(const arm of s.arms){
        /* motes flowing hub -> head */
        for(const m of arm.motes){
          const f=reduced?m.ph:(t/arm.per+m.ph)%1;
          const [dx,dy]=armPos(arm,f,rot);
          ember(gc,s.sprite,color,cx+dx,cy+dy,
            s.moteR*(.7+.6*f),m.aa*Math.min(1,f*10)*(.30+.60*f));
        }
        /* the comet head — one bloom per repository */
        const [hx,hy]=armPos(arm,1,rot);
        const br=reduced?.6:.6+.4*Math.sin(t*TAU/6+arm.ph);
        drawGlow(gc,s.sprite,cx+hx,cy+hy,s.headR*(1+.25*br),.55+.25*br);
        drawGlow(gc,s.sprite,cx+hx,cy+hy,s.headR*.45,.9);
        gc.beginPath();gc.arc(cx+hx,cy+hy,2.6+br*.8,0,TAU);
        gc.fillStyle=hexA(tint(color,.9),.95);gc.fill();
        for(const b of arm.branches){
          const pu=reduced?.8:.72+.28*Math.sin(t*TAU/5+b.ph);
          ember(gc,s.sprite,color,cx+b.ex,cy+b.ey,2.6,pu,.88);
        }
      }
      /* task satellites: size + brightness = freshness */
      for(const o of s.orbs){
        const a=o.ang+rot;
        const x=cx+Math.cos(a)*s.Rorb,y=cy+Math.sin(a)*s.Rorb;
        const pu=reduced?.85:.8+.2*Math.sin(t*TAU/7+o.ph);
        const b=(.42+.58*o.fresh)*pu;
        drawGlow(gc,s.sprite,x,y,s.orbR*(1+1.1*o.fresh),.9*b);
        ember(gc,s.sprite,color,x,y,2.2+2.4*o.fresh,b,.85);
      }
      /* two slow faint glints patrolling the orbit — dimmer than any orb */
      if(!reduced)for(let k=0;k<2;k++){
        const a=-Math.PI/2+(t/34)*TAU+k*Math.PI+rot;
        drawGlow(gc,s.sprite,cx+Math.cos(a)*s.Rorb,
          cy+Math.sin(a)*s.Rorb,3.6,.16);
      }
      /* the breathing white-hot hub */
      const breath=reduced?.5:.5+.5*Math.sin(t*TAU/5);
      drawGlow(gc,s.sprite,cx,cy,s.coreR*2.1+breath*10,.30);
      drawGlow(gc,s.sprite,cx,cy,s.coreR+breath*8,.85);
      drawGlow(gc,s.sprite,cx,cy,s.coreR*.42,.95);
      gc.beginPath();gc.arc(cx,cy,3.4+breath*.9,0,TAU);
      gc.fillStyle=hexA(tint(color,.92),.95);gc.fill();
    });

    if(expanded){
      /* soft scrim so strand roots dim behind the hero figure */
      const ny=cy+56;
      const scr=gc.createRadialGradient(cx,ny,0,cx,ny,80);
      scr.addColorStop(0,'rgba(7,8,10,.78)');
      scr.addColorStop(1,'rgba(7,8,10,0)');
      gc.save();gc.translate(cx,ny);gc.scale(1,.42);gc.translate(-cx,-ny);
      gc.fillStyle=scr;gc.beginPath();gc.arc(cx,ny,80,0,TAU);gc.fill();
      gc.restore();
      text(gc,String(s.orbs.length),cx,cy+60,{font:`500 26px ${SERIF}`,col:INK});
      text(gc,'AGENT TASKS',cx,cy+76,{font:`400 8px ${MONO}`,col:INK3,track:.16});
      if(!s.nWt)
        text(gc,'WORKTREES · NONE',cx,cy-s.Rwt+3.5,
          {font:`400 8px ${MONO}`,col:INK3,track:.16});
      for(const L of s.headLabels){
        text(gc,L.arm.repo.name,L.x+px,L.y+py,
          {font:`400 9.5px ${MONO}`,col:INK2,align:L.align});
        text(gc,'repo · '+(L.arm.branches.length||'no')+' worktree'
          +(L.arm.branches.length===1?'':'s'),L.x+px,L.y+py+12,
          {font:`400 8px ${MONO}`,col:INK3,align:L.align});
      }
      for(const L of s.orbLabels){
        text(gc,fmtAge(L.o.task.date),L.x+px,L.y+py,
          {font:`400 9px ${MONO}`,col:INK2,align:L.align});
        text(gc,L.o.task.name.slice(0,8),L.x+px,L.y+py+11,
          {font:`400 8px ${MONO}`,col:INK3,align:L.align});
      }
      for(const arm of s.arms)for(const b of arm.branches)
        text(gc,b.name,s.cx+b.ex+px,s.cy+b.ey-10+py,
          {font:`400 8px ${MONO}`,col:INK2,alpha:.9});
    }
  },

  hits(s,env){
    const out=[{x:s.cx,y:s.cy,r:30,
      tip:{kick:'q6 · agent workspaces',title:s.arms.length+' repositories',
        sub:s.orbs.length+' agent task workspaces in orbit',
        rows:[['Worktrees',s.nWt?String(s.nWt):'none right now']]}}];
    for(const arm of s.arms){
      out.push({x:s.cx+arm.hx,y:s.cy+arm.hy,r:20,
        tip:{kick:'repository',title:arm.repo.name,
          rows:[['Worktrees',arm.branches.length
            ?String(arm.branches.length):'none right now']]}});
      for(const b of arm.branches)
        out.push({x:s.cx+b.ex,y:s.cy+b.ey,r:14,
          tip:{kick:'worktree',title:b.name,rows:[['Repo',arm.repo.name]]}});
    }
    for(const o of s.orbs)
      out.push({x:s.cx+Math.cos(o.ang)*s.Rorb,
        y:s.cy+Math.sin(o.ang)*s.Rorb,r:16,
        tip:{kick:'agent task workspace',title:o.task.name,
          rows:[['Touched',fmtDate(o.task.date)],
            ['Age',fmtAge(o.task.date)]]}});
    return out;
  }
};
