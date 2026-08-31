/* Shared vocabulary for the Dashboard's eight card renderers: design tokens,
   color math, cached glow sprites, and small drawing helpers. Every card
   draws with these so the eight visualizations read as one system —
   single accent hue per card, white-hot additive cores, ink-token text.

   Cards draw in CSS pixels (the shell owns the dpr transform) with the
   canvas already cleared. The card surface itself is CSS; canvases carry
   only the luminous content. */

export const TAU=Math.PI*2;
export const INK='#e9e4d9', INK2='#b3ada0', INK3='#7d786d';
export const SERIF=`"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif`;
export const SANS=`system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif`;
export const MONO=`ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace`;

/* the commit-heat ramp (q4 beads) — validated ordinal ramp (monotone L, single hue) */
export const HEAT_RAMP=['#1b4f85','#2a6fbd','#4a8fdd','#79b3f0','#b3d7fa'];

const clamp01=v=>v<0?0:v>1?1:v;
const chan=(h,i)=>parseInt(h.slice(1+i*2,3+i*2),16);
export const hexA=(h,a)=>`rgba(${chan(h,0)},${chan(h,1)},${chan(h,2)},${clamp01(a)})`;
export function mix(h1,h2,t){
  t=clamp01(t);
  const v=i=>Math.round(chan(h1,i)+(chan(h2,i)-chan(h1,i))*t)
    .toString(16).padStart(2,'0');
  return '#'+v(0)+v(1)+v(2);
}
export const tint=(h,t)=>mix(h,'#ffffff',t);   /* toward the white-hot core */
export const shade=(h,t)=>mix(h,'#07080a',t);  /* toward the page black */

/* deterministic 0..1 hash — layout jitter that survives reloads */
export const fnv=s=>{
  let h=2166136261;
  for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619);}
  return (h>>>0)/4294967296;
};

export const easeInOut=t=>t<.5?4*t*t*t:1-Math.pow(-2*t+2,3)/2;
export const easeOut=t=>1-Math.pow(1-t,3);

/* ---- glow sprites ----
   One cached 64px offscreen radial sprite per (color, coreTint) pair:
   white-tinted center falling off through the accent to transparent.
   Draw with drawGlow under withAdditive for the originkit-style bloom. */
const spriteCache=new Map();
export function glowSprite(color,coreTint=.75){
  const key=color+'/'+coreTint;
  let sprite=spriteCache.get(key);
  if(sprite)return sprite;
  const S=64,c=document.createElement('canvas');
  c.width=S;c.height=S;
  const g=c.getContext('2d');
  const grd=g.createRadialGradient(S/2,S/2,0,S/2,S/2,S/2);
  grd.addColorStop(0,hexA(tint(color,coreTint),.9));
  grd.addColorStop(.25,hexA(color,.55));
  grd.addColorStop(.6,hexA(color,.13));
  grd.addColorStop(1,hexA(color,0));
  g.fillStyle=grd;g.fillRect(0,0,S,S);
  spriteCache.set(key,c);
  return c;
}
export function drawGlow(gc,sprite,x,y,r,a=1){
  if(a<=0)return;
  gc.globalAlpha=a;
  gc.drawImage(sprite,x-r,y-r,r*2,r*2);
  gc.globalAlpha=1;
}
export function withAdditive(gc,fn){
  const prev=gc.globalCompositeOperation;
  gc.globalCompositeOperation='lighter';
  fn();
  gc.globalCompositeOperation=prev;
}

/* crisp core dot over its halo — the standard "particle" */
export function ember(gc,sprite,color,x,y,r,a=1,coreTint=.8){
  drawGlow(gc,sprite,x,y,r*3.2,a);
  gc.beginPath();gc.arc(x,y,r,0,TAU);
  gc.fillStyle=hexA(tint(color,coreTint),Math.min(1,a*1.15));gc.fill();
}

/* a 4-point star glint: halo + cross flares + core */
export function glint(gc,sprite,color,x,y,r,a=1){
  drawGlow(gc,sprite,x,y,r*3.4,a*.9);
  gc.strokeStyle=hexA(tint(color,.85),a*.75);
  gc.lineWidth=1;
  gc.beginPath();
  gc.moveTo(x-r*2.4,y);gc.lineTo(x+r*2.4,y);
  gc.moveTo(x,y-r*2.4);gc.lineTo(x,y+r*2.4);
  gc.stroke();
  gc.beginPath();gc.arc(x,y,r*.75,0,TAU);
  gc.fillStyle=hexA(tint(color,.9),a);gc.fill();
}

/* soft luminous stroke: wide faint pass under a thin bright core */
export function softLine(gc,x0,y0,x1,y1,color,w=1.4,a=.8){
  gc.lineCap='round';
  gc.strokeStyle=hexA(color,a*.22);
  gc.lineWidth=w*3.4;
  gc.beginPath();gc.moveTo(x0,y0);gc.lineTo(x1,y1);gc.stroke();
  gc.strokeStyle=hexA(tint(color,.45),a);
  gc.lineWidth=w;
  gc.beginPath();gc.moveTo(x0,y0);gc.lineTo(x1,y1);gc.stroke();
}

/* soft luminous ring */
export function softRing(gc,x,y,r,color,w=1.2,a=.5){
  gc.strokeStyle=hexA(color,a*.22);
  gc.lineWidth=w*3;
  gc.beginPath();gc.arc(x,y,r,0,TAU);gc.stroke();
  gc.strokeStyle=hexA(tint(color,.35),a);
  gc.lineWidth=w;
  gc.beginPath();gc.arc(x,y,r,0,TAU);gc.stroke();
}

export function text(gc,s,x,y,{font=`400 11px ${SANS}`,col=INK2,align='center',track=0,alpha=1}={}){
  gc.save();
  gc.font=font;
  gc.textAlign=align;
  if(track)gc.letterSpacing=(track*10)+'px';
  gc.globalAlpha=alpha;
  gc.fillStyle=col;
  gc.fillText(s,x,y);
  gc.restore();
}

export const fmtDate=d=>d?new Date(d+'T00:00:00')
  .toLocaleDateString('en-US',{year:'numeric',month:'short',day:'numeric'}):'';
export const ageLabel=s=>s==null?'':s<90?`${s}s ago`:s<5400?`${Math.round(s/60)}m ago`
  :s<172800?`${Math.round(s/3600)}h ago`:`${Math.round(s/86400)}d ago`;
export const daysSince=d=>d?Math.max(0,(Date.now()-+new Date(d+'T00:00:00'))/86400000):null;
/* recency → 0..1 brightness with a gentle week-scale falloff */
export const freshness=d=>{
  const days=daysSince(d);
  return days==null?.25:Math.max(.12,Math.exp(-days/10));
};

/* offscreen layer matching the card size — for static bases composed once */
export function layer(W,H,dpr){
  const c=document.createElement('canvas');
  c.width=Math.max(1,Math.round(W*dpr));
  c.height=Math.max(1,Math.round(H*dpr));
  const g=c.getContext('2d');
  g.setTransform(dpr,0,0,dpr,0,0);
  /* blit assumes the shell's dpr transform is active on gc */
  return {canvas:c,g,blit(gc){gc.drawImage(c,0,0,W,H);}};
}
