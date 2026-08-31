/* The Dashboard: eight data regions as a bento grid of luminous cards —
   one canvas per card, each running its own generative visualization of
   real workspace data from /xo/dashboard.json (schema 2). The card set
   reads as one system (shared tokens + helpers in ./dashboard/lib.js);
   clicking a card expands it to a full-stage detail view with hover
   tooltips. Deep link: #/dashboard?focus=q4.

   ---- Card contract (js/views/dashboard/cards/<kind>.js) ----
   export default {
     kind:'pulsar',
     init(data, env)  -> state   // layout ONCE: deterministic (lib.fnv for
                                 // jitter), compose static bases into
                                 // lib.layer() offscreens; no per-frame alloc
     draw(gc, state, env, t, mouse) // every frame; CSS-px coords, canvas
                                 // pre-cleared, dpr transform active.
                                 // t = seconds; mouse = {x,y} or null.
     hits(state, env) -> [{x,y,r,tip:{kick,title,sub,rows,foot}}] // optional;
                                 // read once per pointermove in expanded mode
   }
   env = {W,H,dpr,color,expanded,reduced,region:{id,label,stat,blurb,count}}
   Rules: single accent hue (env.color) + lib tints; additive glow via
   lib.withAdditive/ember/glint; animated elements <= ~150 per card (bake
   the rest into a static base layer at init); text wears ink tokens. */
import {API_BASE,apiFetch} from '../core/api.js';
import {hexA,tint} from './dashboard/lib.js';
import vault from './dashboard/cards/vault.js';
import orbits from './dashboard/cards/orbits.js';
import pulsar from './dashboard/cards/pulsar.js';
import branches from './dashboard/cards/branches.js';
import watcher from './dashboard/cards/watcher.js';
import forks from './dashboard/cards/forks.js';
import galaxy from './dashboard/cards/galaxy.js';
import treemap from './dashboard/cards/treemap.js';

const RENDERERS={vault,orbits,pulsar,branches,watcher,forks,galaxy,treemap};
const REDUCED=matchMedia('(prefers-reduced-motion: reduce)').matches;
/* the registry normalises the hash to #/dashboard before mount runs, so a
   ?focus=qN deep link must be captured at load time */
const INITIAL_HASH=location.hash;

let rootEl=null;
let cards=[];        /* [{region,renderer,el,cv,gc,env,state,mouse}] */
let expanded=null;   /* {card,cv,gc,env,state,hitList} while the overlay is open */
let active=false,raf=0;
const T0=performance.now();

const esc=s=>String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

export default {
  id:'dashboard',label:'Dashboard',order:0,
  async mount(el,_ctx){
    rootEl=el;
    const res=await apiFetch(API_BASE+'/xo/dashboard.json');
    if(!res.ok||(res.data&&res.data.schema!==2)){renderNoData(el,res);return;}
    build(el,res.data);
    addEventListener('resize',()=>{if(rootEl)requestAnimationFrame(fitAllCards);});
    addEventListener('keydown',e=>{
      if(!active)return;
      if(e.key==='Escape'&&expanded)collapse();
    });
    const focus=INITIAL_HASH.match(/focus=(q[1-8])\b/);
    if(focus){
      const card=cards.find(c=>c.region.id===focus[1]);
      if(card)expand(card);
    }
  },
  show(){
    active=true;
    fitAllCards();
    if(!raf)raf=requestAnimationFrame(frame);
  },
  hide(){active=false;hideTip();}
};

function renderNoData(el,res){
  const box=document.createElement('div');
  box.className='nodata';
  box.innerHTML='<div class="eyebrow">No data source</div>'+
    '<h1>The Dashboard reads its regions from a local file.</h1>'+
    '<p>This page loads <b>'+API_BASE+'/xo/dashboard.json</b> (schema 2) — a file in the workspace <b>.xo</b> directory, maintained by the watcher. '+
    (res&&res.ok?'The server answered with an older schema; restart it to pick up the new builder:':'Start the workspace server:')+'</p>'+
    '<pre>cd xo-cowork-api && ./cowork-api.sh start</pre>'+
    '<button id="dnodata-retry">Retry</button>';
  el.appendChild(box);
  box.querySelector('#dnodata-retry').addEventListener('click',()=>location.reload());
}

/* ============================== GRID ============================== */
function build(el,DATA){
  el.innerHTML=`<div class="dgrid"></div>
    <div class="dxp" hidden>
      <header>
        <span class="ddot"></span>
        <b class="dxp-name"></b>
        <span class="dxp-stat"></span>
        <button class="dxp-close" title="Close (Esc)">&#10005;</button>
      </header>
      <div class="dxp-body"><canvas></canvas></div>
      <footer class="dxp-blurb"></footer>
    </div>`;
  const grid=el.querySelector('.dgrid');
  cards=(DATA.regions||[]).map(region=>{
    const renderer=RENDERERS[region.kind];
    const fig=document.createElement('figure');
    fig.className='dcard';
    fig.dataset.region=region.id;
    fig.style.setProperty('--accent',region.color);
    fig.innerHTML=`<div class="dframe"><canvas></canvas></div>
      <figcaption>
        <span class="ddot"></span>
        <b class="dname">${esc(region.label)}</b>
        <span class="dstat">${esc(region.stat)}</span>
      </figcaption>`;
    grid.appendChild(fig);
    const cv=fig.querySelector('canvas');
    const card={region,renderer,el:fig,cv,gc:cv.getContext('2d'),
      env:null,state:null,mouse:null};
    cv.addEventListener('pointermove',e=>{
      const r=cv.getBoundingClientRect();
      card.mouse={x:e.clientX-r.left,y:e.clientY-r.top};
      if(REDUCED)renderCard(card);
    });
    cv.addEventListener('pointerleave',()=>{card.mouse=null;if(REDUCED)renderCard(card);});
    fig.addEventListener('click',()=>expand(card));
    return card;
  });
}

function fitCard(card){
  const box=card.cv.parentElement.getBoundingClientRect();
  const W=Math.round(box.width),H=Math.round(box.height);
  if(W<10||H<10)return;
  const dpr=Math.min(2,devicePixelRatio||1);
  if(card.env&&card.env.W===W&&card.env.H===H&&card.env.dpr===dpr)return;
  card.cv.width=W*dpr;card.cv.height=H*dpr;
  card.env={W,H,dpr,color:card.region.color,expanded:false,reduced:REDUCED,
    region:{id:card.region.id,label:card.region.label,stat:card.region.stat,
      blurb:card.region.blurb,count:card.region.count}};
  try{card.state=card.renderer?card.renderer.init(card.region.data,card.env):null;}
  catch(err){console.error('card '+card.region.id+' init failed:',err);card.state=null;}
  if(REDUCED)renderCard(card);
}
function fitAllCards(){cards.forEach(fitCard);if(expanded)fitExpanded();}

function renderCard(card){
  if(!card.env||!card.state)return;
  const {gc,env}=card;
  gc.setTransform(env.dpr,0,0,env.dpr,0,0);
  gc.clearRect(0,0,env.W,env.H);
  const t=REDUCED?12:(performance.now()-T0)/1000;
  try{card.renderer.draw(gc,card.state,env,t,card.mouse);}
  catch(err){console.error('card '+card.region.id+' draw failed:',err);card.state=null;}
}

function frame(){
  raf=0;
  if(!active)return;
  if(!REDUCED){
    if(expanded)renderExpanded();
    else cards.forEach(renderCard);
  }
  raf=requestAnimationFrame(frame);
}

/* ============================== EXPANDED ============================== */
function expand(card){
  const xp=rootEl.querySelector('.dxp');
  xp.hidden=false;
  xp.style.setProperty('--accent',card.region.color);
  xp.querySelector('.dxp-name').textContent=card.region.label;
  xp.querySelector('.dxp-stat').textContent=card.region.stat;
  xp.querySelector('.dxp-blurb').textContent=card.region.blurb;
  const cv=xp.querySelector('canvas');
  expanded={card,cv,gc:cv.getContext('2d'),env:null,state:null,hitList:[]};
  xp.querySelector('.dxp-close').onclick=collapse;
  cv.onpointermove=e=>{
    const r=cv.getBoundingClientRect();
    const mx=e.clientX-r.left,my=e.clientY-r.top;
    expanded.mouse={x:mx,y:my};
    let best=null,bd=1e9;
    for(const h of expanded.hitList){
      const d=Math.hypot(h.x-mx,h.y-my);
      if(d<Math.max(h.r,14)&&d<bd){bd=d;best=h;}
    }
    cv.style.cursor=best?'pointer':'default';
    if(best)showTip(best.tip,card.region.color,e.clientX,e.clientY);
    else hideTip();
  };
  cv.onpointerleave=()=>{expanded.mouse=null;hideTip();};
  fitExpanded();
  history.replaceState(null,'','#/dashboard?focus='+card.region.id);
}
function fitExpanded(){
  if(!expanded)return;
  const box=expanded.cv.parentElement.getBoundingClientRect();
  const W=Math.round(box.width),H=Math.round(box.height);
  if(W<10||H<10)return;
  const dpr=Math.min(2,devicePixelRatio||1);
  expanded.cv.width=W*dpr;expanded.cv.height=H*dpr;
  const src=expanded.card;
  expanded.env={W,H,dpr,color:src.region.color,expanded:true,reduced:REDUCED,
    region:{id:src.region.id,label:src.region.label,stat:src.region.stat,
      blurb:src.region.blurb,count:src.region.count}};
  try{
    expanded.state=src.renderer?src.renderer.init(src.region.data,expanded.env):null;
    expanded.hitList=(src.renderer&&src.renderer.hits&&expanded.state)
      ?(src.renderer.hits(expanded.state,expanded.env)||[]):[];
  }catch(err){console.error('expand '+src.region.id+' failed:',err);expanded.state=null;}
  if(REDUCED)renderExpanded();
}
function renderExpanded(){
  if(!expanded||!expanded.env||!expanded.state)return;
  const {gc,env}=expanded;
  gc.setTransform(env.dpr,0,0,env.dpr,0,0);
  gc.clearRect(0,0,env.W,env.H);
  const t=REDUCED?12:(performance.now()-T0)/1000;
  try{expanded.card.renderer.draw(gc,expanded.state,env,t,expanded.mouse||null);}
  catch(err){console.error('expanded draw failed:',err);expanded.state=null;}
}
function collapse(){
  rootEl.querySelector('.dxp').hidden=true;
  expanded=null;
  hideTip();
  history.replaceState(null,'','#/dashboard');
}

/* ============================== HOVER CARD ============================== */
const hcEl=()=>document.getElementById('hc');
function showTip(tip,color,mx,my){
  const hc=hcEl();
  if(!hc||!tip)return;
  hc.innerHTML=`
    <div class="art" style="background:linear-gradient(155deg, ${hexA(color,.24)}, ${hexA(color,.03)} 68%)">
      <div class="kicker">${esc(tip.kick||'')}</div>
      <h5>${esc(tip.title||'')}</h5>
      ${tip.sub?`<div class="sub">${esc(tip.sub)}</div>`:''}
    </div>
    ${tip.rows&&tip.rows.length?`<dl>${tip.rows.filter(r=>r&&r[1]).map(([dt,dd])=>`<dt>${esc(dt)}</dt><dd>${esc(dd)}</dd>`).join('')}</dl>`:''}
    ${tip.foot?`<div class="foot">${esc(tip.foot)}</div>`:''}`;
  hc.classList.add('is-on');
  const r=hc.getBoundingClientRect();
  let x=mx+18,y=my+18;
  if(x+r.width>innerWidth-8)x=mx-r.width-18;
  if(y+r.height>innerHeight-8)y=my-r.height-18;
  hc.style.left=Math.max(8,x)+'px';hc.style.top=Math.max(64,y)+'px';
}
function hideTip(){hcEl()?.classList.remove('is-on');}
