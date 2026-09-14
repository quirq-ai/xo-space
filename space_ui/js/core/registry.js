/* View registry: maps primary sections to their default pages, assigns hotkeys
   1..n (ignored while an input, textarea or select has focus), syncs the URL
   hash (#/<route>, deep-linkable), lazy-mounts each view on first activation,
   and isolates a view's failure to its own section: the other tabs keep
   working. The registry knows the view contract, never the
   views themselves (the same seam philosophy as the backend's capability
   loader).

   View contract (js/views/*.js default export or named export):
     {
       id: 'sessions',          // section is #view-<id>, tab is #tab-<id>
       route: 'sessions',       // optional canonical URL path; defaults to id
       aliases: [],             // additional accepted URL paths
       label: 'Sessions',       // tab text (may contain entities)
       order: 4,                // nav position; hotkey is its 1-based index
       nav: true,               // legacy fallback when no primary tabs are supplied
       parent: null,            // parent tab highlighted for a child view
       section: null,           // optional shared section id (without view-)
       toolbar: null,           // descriptor/function: graph controls or local search
       async mount(el, ctx) {}, // first activation; el is the section
       show() {}, hide() {},    // optional, on tab switches
       async refresh() {},     // optional, reload this page's data in place
     }
   The section is created inside #stage automatically when index.html does
   not already carry one: markup-heavy views keep theirs in index.html,
   render-everything views need no HTML edit at all.
   startRegistry({tabs,defaultView}) receives explicit primary navigation;
   each tab has {id,label,defaultView,aliases?}. Page labels and physical DOM
   sections are independent of that top-level navigation.
   ctx = {switchTo, refreshToolbar}. Views never import each other; cross-view jumps go
   through ctx.switchTo(id or route). */

let views=[];
const byId=new Map();
const byRoute=new Map();
let current=null;
let activation=0;
const byTab=new Map();
const refreshing=new Map();

export function registerView(v){
  if(byId.has(v.id))views=views.map(w=>w.id===v.id?v:w); /* idempotent re-register */
  else views.push(v);
  byId.set(v.id,v);
  /* Re-registration replaces its route contract, including removed aliases. */
  byRoute.clear();
  for(const view of views){
    byRoute.set(view.route||view.id,view);
    for(const alias of view.aliases||[])byRoute.set(alias,view);
  }
}

const resolveView=id=>{
  const target=byTab.get(id)?.defaultView||id;
  return byId.get(target)||byRoute.get(target);
};
const viewHash=v=>'#/'+(v.route||v.id);

const ctx={switchTo};
function refreshToolbar(v){
  if(current===v.id)dispatchEvent(new CustomEvent('space:toolbar',{
    detail:{id:v.id,toolbar:v.toolbar||null}
  }));
}

export async function switchTo(target,{replace=false}={}){
  const v=resolveView(target);
  if(!v)return false;
  const id=v.id;
  const request=++activation;
  const prev=current&&current!==id?byId.get(current):null;
  current=id;
  /* Let the outgoing page record scroll and drafts while its DOM is still
     visible. display:none can reset scroll offsets before hide() reads them. */
  if(prev&&prev.hide){
    try{prev.hide();}catch(err){console.error('view "'+prev.id+'" hide failed:',err);}
  }
  const activeTab=v.parent||v.id;
  const activeSection=v.section||v.id;
  const sectionIds=new Set(views.map(w=>w.section||w.id));
  for(const sectionId of sectionIds){
    document.getElementById('view-'+sectionId)?.classList.toggle(
      'is-active',
      sectionId===activeSection,
    );
  }
  for(const tab of document.querySelectorAll('.tabs [id^="tab-"]')){
    const on=tab.id==='tab-'+activeTab;
    tab.classList.toggle('is-on',on);
    if(on)tab.setAttribute('aria-current','page');else tab.removeAttribute('aria-current');
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
  const hash=viewHash(v);
  if(location.hash!==hash)history[replace?'replaceState':'pushState'](null,'',hash);
  /* Announce the section and page before awaiting work. Shared navigation,
     toolbar and preview context must follow the URL even during a slow load. */
  dispatchEvent(new CustomEvent('space:view',{detail:{id,tab:activeTab,section:activeSection,
    route:v.route||v.id,label:v.label,toolbar:v.toolbar||null,
    refreshable:typeof v.refresh==='function',refreshing:refreshing.has(id)}}));
  if(!v.mounted){
    v.mounted=true; /* idempotent mount: activating N times mounts once */
    const el=document.getElementById('view-'+(v.section||v.id));
    v.mountPromise=(async()=>{
      try{
        await v.mount(el,{...ctx,refreshToolbar:()=>refreshToolbar(v)});
        return true;
      }catch(err){
        console.error('view "'+v.id+'" failed to mount:',err);
        renderMountError(el,v);
        return false;
      }
    })();
  }
  /* Reentry shares the pending mount. Only the latest navigation may show
     it: a slow mount must not reactivate a page the user already left. */
  if(await v.mountPromise===false||request!==activation)return false;
  if(v.show){
    try{await v.show();}
    catch(err){console.error('view "'+v.id+'" show failed:',err);return false;}
  }
  if(request!==activation)return false;
  refreshToolbar(v);
  return true;
}

/* The shell owns the button and its busy state; each page owns what to read
   and which local state to retain. Waiting for mount prevents an early click
   from refreshing uninitialized DOM. A later navigation cancels that wait. */
export function refreshCurrentView(){
  const v=byId.get(current);
  if(typeof v?.refresh!=='function')return Promise.resolve(false);
  if(refreshing.has(v.id))return refreshing.get(v.id);
  const request=activation;
  const promise=Promise.resolve().then(async()=>{
    if(await v.mountPromise===false||request!==activation)return false;
    await v.refresh();
    return true;
  }).finally(()=>{
    refreshing.delete(v.id);
    dispatchEvent(new CustomEvent('space:refresh-state',{detail:{id:v.id,busy:false}}));
  });
  refreshing.set(v.id,promise);
  dispatchEvent(new CustomEvent('space:refresh-state',{detail:{id:v.id,busy:true}}));
  return promise;
}

export function startRegistry({defaultView,tabs:tabDefinitions}){
  views.sort((a,b)=>(a.order||0)-(b.order||0));
  const navViews=views.filter(v=>v.nav!==false);
  const navigation=tabDefinitions||navViews.map(v=>({id:v.id,label:v.label,defaultView:v.id}));
  byTab.clear();
  for(const tab of navigation){
    byTab.set(tab.id,tab);
    for(const alias of tab.aliases||[])byTab.set(alias,tab);
  }
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
  if(tabs)tabs.replaceChildren(...navigation.map(tab=>{
    const b=document.createElement('a');
    b.id='tab-'+tab.id;
    b.href=viewHash(resolveView(tab.defaultView));
    b.textContent=tab.label;
    b.addEventListener('click',event=>{
      if(event.button||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;
      event.preventDefault();switchTo(tab.defaultView);
    });
    return b;
  }));
  addEventListener('keydown',e=>{
    /* a digit typed into a field, a textarea or a select menu is input, not
       a tab switch */
    if(/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName||''))return;
    if(e.key.length!==1||e.key<'1'||e.key>'9')return;
    const i=e.key.charCodeAt(0)-49;
    if(i<navigation.length)switchTo(navigation[i].defaultView);
  });
  addEventListener('hashchange',()=>{
    const id=location.hash.replace(/^#\//,'');
    const view=resolveView(id);
    if(view&&(view.id!==current||location.hash!==viewHash(view)))switchTo(id,{replace:true});
  });
  const initial=location.hash.replace(/^#\//,'');
  switchTo(resolveView(initial)?initial:defaultView,{replace:true});
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
