/* View registry: builds the tab nav from registered views, assigns hotkeys
   1..n (ignored while an input, textarea or select has focus), syncs the URL
   hash (#/<id>, deep-linkable), lazy-mounts each view on first activation,
   and isolates a view's failure to its own section: the other tabs keep
   working. The registry knows the view contract, never the
   views themselves (the same seam philosophy as the backend's capability
   loader).

   View contract (js/views/*.js default export or named export):
     {
       id: 'sessions',          // section is #view-<id>, tab is #tab-<id>
       label: 'Sessions',       // tab text (may contain entities)
       order: 4,                // nav position; hotkey is its 1-based index
       nav: true,               // false keeps a child view out of the top nav
       parent: null,            // parent tab highlighted for a child view
       section: null,           // optional shared section id (without view-)
       async mount(el, ctx) {}, // first activation; el is the section
       show() {}, hide() {},    // optional, on tab switches
     }
   The section is created inside #stage automatically when index.html does
   not already carry one: markup-heavy views keep theirs in index.html,
   render-everything views need no HTML edit at all.
   ctx = {switchTo}. Views never import each other; cross-view jumps go
   through ctx.switchTo(id). */

let views=[];
const byId=new Map();
let current=null;

export function registerView(v){
  if(byId.has(v.id))views=views.map(w=>w.id===v.id?v:w); /* idempotent re-register */
  else views.push(v);
  byId.set(v.id,v);
}

const ctx={switchTo};

export async function switchTo(id){
  const v=byId.get(id);
  if(!v)return;
  const prev=current&&current!==id?byId.get(current):null;
  current=id;
  const activeTab=v.parent||v.id;
  const activeSection=v.section||v.id;
  const sectionIds=new Set(views.map(w=>w.section||w.id));
  for(const sectionId of sectionIds){
    document.getElementById('view-'+sectionId)?.classList.toggle(
      'is-active',
      sectionId===activeSection,
    );
  }
  for(const w of views){
    document.getElementById('tab-'+w.id)?.classList.toggle('is-on',w.id===activeTab);
  }
  /* The stage clips its stacked sections, but hidden overflow can still be
     scrolled programmatically (e.g. by focus scrolls); pin it back. */
  const stage=document.getElementById('stage');
  if(stage){stage.scrollLeft=0;stage.scrollTop=0;}
  requestAnimationFrame(()=>{
    document.getElementById('tab-'+activeTab)?.scrollIntoView({
      block:'nearest',
      inline:'nearest',
    });
  });
  history.replaceState(null,'','#/'+id);
  /* Shell chrome (the Files lens switch) needs to know which view is active
     without importing views. activeTab is the parent for a child view, so a
     lens and its parent tab report the same tab. */
  dispatchEvent(new CustomEvent('space:view',{detail:{id,tab:activeTab}}));
  if(prev&&prev.hide){
    try{prev.hide();}catch(err){console.error('view "'+prev.id+'" hide failed:',err);}
  }
  if(!v.mounted){
    v.mounted=true; /* idempotent mount: activating N times mounts once */
    const el=document.getElementById('view-'+(v.section||v.id));
    try{await v.mount(el,ctx);}
    catch(err){
      console.error('view "'+v.id+'" failed to mount:',err);
      renderMountError(el,v);
      return;
    }
  }
  if(v.show){
    try{v.show();}catch(err){console.error('view "'+v.id+'" show failed:',err);}
  }
}

export function startRegistry({defaultView}){
  views.sort((a,b)=>(a.order||0)-(b.order||0));
  const navViews=views.filter(v=>v.nav!==false);
  const stage=document.getElementById('stage');
  for(const v of views){
    const sectionId=v.section||v.id;
    if(stage&&!document.getElementById('view-'+sectionId)){
      const s=document.createElement('section');
      s.className='view';s.id='view-'+sectionId;
      stage.appendChild(s);
    }
  }
  const tabs=document.querySelector('.tabs');
  if(tabs)tabs.replaceChildren(...navViews.map(v=>{
    const b=document.createElement('button');
    b.id='tab-'+v.id;
    b.innerHTML=v.label;
    b.addEventListener('click',()=>switchTo(v.id));
    return b;
  }));
  addEventListener('keydown',e=>{
    /* a digit typed into a field, a textarea or a select menu is input, not
       a tab switch */
    if(/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName||''))return;
    if(e.key.length!==1||e.key<'1'||e.key>'9')return;
    const i=e.key.charCodeAt(0)-49;
    if(i<navViews.length)switchTo(navViews[i].id);
  });
  addEventListener('hashchange',()=>{
    const id=location.hash.replace(/^#\//,'');
    if(byId.has(id)&&id!==current)switchTo(id);
  });
  const initial=location.hash.replace(/^#\//,'');
  switchTo(byId.has(initial)?initial:defaultView);
}

/* per-view bulkhead: a throwing mount gets an error card in its own section */
function renderMountError(el,v){
  if(!el)return;
  const box=document.createElement('div');
  box.style.cssText='position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;padding:32px';
  box.innerHTML='<div>'
    +'<div style="font:400 10px ui-monospace,monospace;letter-spacing:.14em;color:#7d786d">VIEW FAILED</div>'
    +'<p style="max-width:44ch;color:#b3ada0;font-size:14px;margin:10px 0 0">The '+v.label
    +' view hit an error and was isolated. The other tabs keep working. Details are in the browser console.</p>'
    +'</div>';
  el.appendChild(box);
}
