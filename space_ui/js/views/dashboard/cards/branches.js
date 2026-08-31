/* q4 · Git History — "Branch Constellations".
   One CLUSTER per repository — the point of the card is that a project's
   branches belong together, and the gaps between clusters are as legible
   as the clusters themselves. Each cluster is a soft-glowing hub (the
   repo) with its branches raying outward as radial timelines: radius is
   time, hub edge = the start of the 16-week window, outer ring = today,
   so an actively pushed branch physically reaches the rim. Commit days
   bead each ray as embers (log2 heat, one scale shared across every
   cluster), the tip ember marks the branch's last commit, HEAD carries a
   pulsing ring, and the default branch draws a shade brighter. Tags sit
   on a lower arc as diamonds at their date's radius. Expanded adds
   per-branch labels at the ray tips, month rings inside each cluster,
   and hub captions; hovering a tip reports sha, last commit, commits in
   window and ahead/behind the default. Boundaries, rays, cold embers,
   diamonds and labels are baked into a lib.layer() at init; the freshest
   tips breathe and a comet runs the HEAD ray of the youngest repo. */
import {TAU,INK2,INK3,MONO,hexA,tint,fnv,glowSprite,drawGlow,withAdditive,
  ember,softRing,text,layer,fmtDate}from'../lib.js';

const DAYS=112;                       /* 16 weeks, matching the builder */
const SHIM_N=8;                       /* freshest branch tips that breathe */

const pad2=n=>String(n).padStart(2,'0');
const iso=d=>d.getFullYear()+'-'+pad2(d.getMonth()+1)+'-'+pad2(d.getDate());
const dayU=(ds,end)=>{                /* 0 at window start → 1 today */
  const ms=new Date(ds+'T00:00:00')-new Date(end+'T00:00:00');
  return Math.max(0,Math.min(1,(DAYS-1+Math.round(ms/86400000))/(DAYS-1)));
};

export default{
  kind:'branches',

  init(data,env){
    const {W,H,dpr,color,expanded}=env;
    const repos=((data&&data.repos)||[]).slice(0,expanded?8:4);
    const empty=!repos.length;
    const end=(data&&data.end)||iso(new Date());
    const perRepo=expanded?8:5;

    /* ---- cluster layout: a loose grid, one cell per repository ---- */
    const n=Math.max(1,repos.length);
    const cols=Math.max(1,Math.round(Math.sqrt(n*W/Math.max(1,H))));
    const rows=Math.ceil(n/cols);
    const cellW=W/Math.min(cols,n),cellH=H/rows;
    const labelH=expanded?30:16;
    const R=Math.max(18,Math.min(cellW,cellH)/2-labelH-(expanded?14:6));
    const r0=R*.22;                   /* hub edge: the window opens here */
    const radial=u=>r0+(R-r0)*u;
    const sprite=glowSprite(color);

    /* ---- shared log2 heat scale across every cluster's commit days ---- */
    const maxN=Math.max(1,...repos.flatMap(r=>(r.branches||[])
      .flatMap(b=>(b.days||[]).map(d=>d.n||0))));
    const lg=Math.log2(1+maxN);
    const rMax=Math.min(R*.06+2,expanded?5:3.2);

    const beads=[],tips=[],diamonds=[],hubs=[];
    const base=layer(W,H,dpr);
    const g=base.g;

    (empty?[{name:'no repos',branches:[],tags:[]}]:repos).forEach((repo,ri)=>{
      const cx=(ri%cols+.5)*cellW+(cellW*(ri%2?.02:-.02));
      const cy=Math.floor(ri/cols)*cellH+cellH/2-labelH*.4;
      const branches=(repo.branches||[]).slice(0,perRepo);

      /* the cluster's ground: a soft backdrop glow, a dotted today-ring,
         and (expanded) faint month rings — radius IS the time axis */
      drawGlow(g,sprite,cx,cy,R*2.4,.05);
      g.strokeStyle='rgba(233,228,217,.09)';
      g.setLineDash([1.5,4.5]);g.lineWidth=1;
      g.beginPath();g.arc(cx,cy,R,0,TAU);g.stroke();
      g.setLineDash([]);
      if(expanded){
        let prev=-1;
        for(let i=0;i<DAYS;i+=7){
          const d=new Date(end+'T00:00:00');d.setDate(d.getDate()-(DAYS-1-i));
          if(d.getMonth()!==prev&&i>0){
            prev=d.getMonth();
            g.strokeStyle='rgba(233,228,217,.045)';
            g.beginPath();g.arc(cx,cy,radial(i/(DAYS-1)),0,TAU);g.stroke();
          }else prev=d.getMonth();
        }
      }
      /* hub core */
      withAdditive(g,()=>{drawGlow(g,sprite,cx,cy,r0*1.9,.5);});

      /* branches ray out; angles spread evenly with a per-name jitter */
      branches.forEach((br,bi)=>{
        const a=-Math.PI/2+bi*(TAU/Math.max(3,branches.length))
          +(fnv(repo.name+'/'+br.name)-.5)*.3;
        const dx=Math.cos(a),dy=Math.sin(a);
        const tipR=radial(dayU(br.tipDate||end,end));
        /* faint full-window guide, brighter active span to the tip */
        g.strokeStyle='rgba(233,228,217,.05)';g.lineWidth=1;
        g.beginPath();g.moveTo(cx+dx*r0,cy+dy*r0);
        g.lineTo(cx+dx*R,cy+dy*R);g.stroke();
        g.strokeStyle=hexA(tint(color,br.isDefault?.62:.45),
          br.isDefault?.55:.34);
        g.lineWidth=br.isDefault?1.6:1.1;
        g.beginPath();g.moveTo(cx+dx*r0,cy+dy*r0);
        g.lineTo(cx+dx*tipR,cy+dy*tipR);g.stroke();
        for(const d of br.days||[]){
          const u=Math.log2(1+(d.n||0))/lg,rr=radial(dayU(d.d,end));
          beads.push({repo:repo.name,branch:br.name,d:d.d,n:d.n,
            x:cx+dx*rr,y:cy+dy*rr,r:rMax*(.35+.65*u),a:.4+.5*u,core:.35+.55*u});
        }
        const tip={repo:repo.name,br,x:cx+dx*tipR,y:cy+dy*tipR,
          hx:cx+dx*r0,hy:cy+dy*r0,      /* the comet runs hub → tip */
          r:Math.max(rMax*.9,expanded?3.2:2.3),
          phase:fnv(repo.name+'/'+br.name)*TAU};
        tips.push(tip);
        if(expanded){
          /* clamped: an edge cluster's outward label must stay on canvas */
          const lx=Math.max(8,Math.min(W-8,cx+dx*(R+10)));
          const ly=cy+dy*(R+10);
          text(g,br.name,lx,ly+3,{font:`400 8.5px ${MONO}`,
            col:br.isHead?INK2:INK3,align:dx<-.25?'right':dx>.25?'left':'center',
            alpha:.95});
        }
      });

      /* tags: diamonds on the lower arc, radius by date */
      (repo.tags||[]).forEach((tag,ti)=>{
        const a=Math.PI*(.32+.36*((ti+.5)/Math.max(1,(repo.tags||[]).length)));
        const rr=radial(dayU(tag.date||end,end));
        const tx=cx+Math.cos(a)*rr,ty=cy+Math.sin(a)*rr;
        const s=expanded?3.6:2.4;
        g.save();g.translate(tx,ty);g.rotate(Math.PI/4);
        g.fillStyle=hexA(tint(color,.75),.85);
        g.fillRect(-s/2,-s/2,s,s);g.restore();
        diamonds.push({repo:repo.name,tag,x:tx,y:ty,r:Math.max(9,s*3)});
      });

      /* the cluster is named — the label is the grouping, spelled out */
      text(g,String(repo.name||''),cx,cy+R+(expanded?20:12),
        {font:`400 ${expanded?10:7.5}px ${MONO}`,col:INK2,track:.06});
      if(expanded&&!empty)
        text(g,(repo.branchTotal??branches.length)+' branches · '
          +(repo.tagTotal??(repo.tags||[]).length)+' tags'
          +(repo.head?' · head '+repo.head:''),cx,cy+R+33,
          {font:`400 8.5px ${MONO}`,col:INK3});
      if(!empty)hubs.push({x:cx,y:cy,r:Math.max(12,r0),
        tip:{kick:'repository',title:repo.name,
          rows:[['Branches',String(repo.branchTotal??branches.length)],
            ['Tags',String(repo.tagTotal??(repo.tags||[]).length)],
            ['HEAD',repo.head||'detached'],
            ['Default',repo.default||'—'],
            ['Hub → rim','16 weeks, today at the ring']]}});
    });

    withAdditive(g,()=>{
      for(const b of beads)ember(g,sprite,color,b.x,b.y,b.r,b.a,b.core);
      for(const p of tips)ember(g,sprite,color,p.x,p.y,p.r,.85,.7);
    });
    if(empty)
      text(g,'NO REPOSITORIES',W/2,H/2,
        {font:`400 8px ${MONO}`,col:INK3,track:.16});

    const shim=[...tips].sort((a,b)=>
      String(b.br.tipDate||'').localeCompare(String(a.br.tipDate||''))).slice(0,SHIM_N);
    const heads=tips.filter(p=>p.br.isHead);
    const comet=heads.length?heads[0]:null;

    return {base,sprite,beads,tips,shim,heads,comet,hubs,diamonds,
      defaultOf:new Map(repos.map(r=>[r.name,r.default]))};
  },

  draw(gc,s,env,t){
    const {color,reduced}=env;
    s.base.blit(gc);
    withAdditive(gc,()=>{
      for(const p of s.shim){
        const b=reduced?1:.8+.2*Math.sin(t*TAU/5.4+p.phase);
        ember(gc,s.sprite,color,p.x,p.y,p.r*(.92+.12*b),.85*b,.7);
      }
      for(const p of s.heads){
        const b=reduced?.5:.5+.5*Math.sin(t*TAU/6+p.phase);
        softRing(gc,p.x,p.y,p.r+3.5,color,1,.22+.26*b);
      }
      if(s.comet&&!reduced){
        const u=(t/8)%1;
        const cx=s.comet.hx+(s.comet.x-s.comet.hx)*u;
        const cy=s.comet.hy+(s.comet.y-s.comet.hy)*u;
        drawGlow(gc,s.sprite,cx,cy,7,.5);
        drawGlow(gc,s.sprite,cx-(s.comet.x-s.comet.hx)*.05,
          cy-(s.comet.y-s.comet.hy)*.05,4,.25);
      }
    });
  },

  hits(s){
    const out=[];
    for(const p of s.tips){
      const br=p.br,def=s.defaultOf.get(p.repo);
      const rows=[['Tip',br.tip||'—'],
        ['Last commit',br.tipDate?fmtDate(br.tipDate):'—'],
        ['Commits (16 wk)',String(br.n||0)]];
      if(br.ahead!=null)rows.push(['vs '+(def||'default'),`+${br.ahead} −${br.behind}`]);
      if(br.isHead)rows.push(['·','checked out (HEAD)']);
      else if(br.isDefault)rows.push(['·','default branch']);
      out.push({x:p.x,y:p.y,r:Math.max(10,p.r+5),
        tip:{kick:'branch · '+p.repo,title:br.name,rows}});
    }
    for(const b of s.beads)
      if(b.r>1.6)out.push({x:b.x,y:b.y,r:Math.max(8,b.r+4),
        tip:{kick:'commit day · '+b.branch,title:fmtDate(b.d),
          rows:[['Commits',String(b.n)],['Repo',b.repo]]}});
    for(const d of s.diamonds)
      out.push({x:d.x,y:d.y,r:d.r,
        tip:{kick:'tag · '+d.repo,title:d.tag.name,
          rows:[['At',d.tag.tip||'—'],['Date',d.tag.date?fmtDate(d.tag.date):'—']]}});
    return out.concat(s.hubs);
  }
};
