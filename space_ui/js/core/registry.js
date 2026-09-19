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
   sections are independent of that top-level navigation. setNavigation(tabs)
   re-applies the primary navigation later (the shell does when /api/ui
   arrives after a fallback boot); unregisterView(id) retires a view (a spec
   page whose module switched off); both, and every registration after the
   start, announce the page list as a 'space:pages' event whose detail
   {views:[{id,route,label,order,parent,section,secondary,sectionNav}]} the
   secondary navigation renders from. Two optional flags a view may carry:
   secondary:false keeps it out of the secondary navigation lists, and
   sectionNav:false hides the shell's secondary navigation while it is
   active (a page that draws its own, like Setup).
   ctx = {switchTo, refreshToolbar}. Views never import each other; cross-view jumps go
   through ctx.switchTo(id or route). */

let views=[];
const byId=new Map();
const byRoute=new Map();
let current=null;
let activation=0;
const byTab=new Map();
const refreshing=new Map();
let navigation=[];
let started=false;
let defaultRoute=null;
/* Sections the registry created itself (a view index.html carries no markup
   for); those are removed again when the view is unregistered. */
const createdSections=new Set();

function rebuildRoutes(){
  byRoute.clear();
  for(const view of views){
    byRoute.set(view.route||view.id,view);
    for(const alias of view.aliases||[])byRoute.set(alias,view);
  }
}

function ensureSection(v){
  const stage=document.getElementById('stage');
  const sectionId=v.section||v.id;
  if(stage&&!document.getElementById('view-'+sectionId)){
    const s=document.createElement('section');
    s.className='view';s.id='view-'+sectionId;
    stage.appendChild(s);
    createdSections.add(sectionId);
  }
}

/* The page list, as data: what the secondary navigation and the command
   palette may list without importing the views. */
export function listViews(){
  return views.map(v=>({id:v.id,route:v.route||v.id,aliases:[...(v.aliases||[])],label:v.label,
    order:v.order||0,parent:v.parent||null,section:v.section||v.id,
    secondary:v.secondary!==false,sectionNav:v.sectionNav!==false,spec:!!v.spec}));
}
function announcePages(){
  if(!started)return;
  dispatchEvent(new CustomEvent('space:pages',{detail:{views:listViews()}}));
}

export function registerView(v){
  if(byId.has(v.id))views=views.map(w=>w.id===v.id?v:w); /* idempotent re-register */
  else views.push(v);
  byId.set(v.id,v);
  /* Re-registration replaces its route contract, including removed aliases. */
  rebuildRoutes();
  if(started){ensureSection(v);announcePages();}
}

/* Retire a view: its routes stop resolving, its section goes when the
   registry created it, and a person looking at it lands on their tab's
   default page (or the app default). Returns false for an unknown id. */
export function unregisterView(id){
  const v=byId.get(id);
  if(!v)return false;
  views=views.filter(w=>w!==v);
  byId.delete(id);
  rebuildRoutes();
  if(current===id&&v.hide){try{v.hide();}catch(err){console.error('view "'+id+'" hide failed:',err);}}
  if(typeof v.destroy==='function'){try{v.destroy();}catch(err){console.error('view "'+id+'" destroy failed:',err);}}
  const sectionId=v.section||v.id;
  if(createdSections.has(sectionId)&&!views.some(w=>(w.section||w.id)===sectionId)){
    document.getElementById('view-'+sectionId)?.remove();
    createdSections.delete(sectionId);
  }
  announcePages();
  if(current===id){
    current=null;
    /* The same route when another view still answers it (a hand-written
       page a spec replaced, or the reverse), else the tab's default page. */
    const route=v.route||v.id;
    const tab=byTab.get(v.parent||v.id);
    const target=resolveView(route)?route:tab&&resolveView(tab.defaultView)?tab.defaultView:defaultRoute;
    if(target)switchTo(target,{replace:true});
  }
  return true;
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

/* Build the tab bar and the tab lookup for one navigation list. Called by
   startRegistry and again by setNavigation; listeners are attached once. */
function applyNavigation(tabDefinitions){
  const navViews=views.filter(v=>v.nav!==false);
  navigation=tabDefinitions||navViews.map(v=>({id:v.id,label:v.label,defaultView:v.id}));
  byTab.clear();
  for(const tab of navigation){
    byTab.set(tab.id,tab);
    for(const alias of tab.aliases||[])byTab.set(alias,tab);
  }
  for(const v of views)ensureSection(v);
  const tabs=document.querySelector('.tabs');
  if(tabs)tabs.replaceChildren(...navigation.map(tab=>{
    const b=document.createElement('a');
    b.id='tab-'+tab.id;
    const target=resolveView(tab.defaultView);
    b.href=target?viewHash(target):'#/'+tab.defaultView;
    b.textContent=tab.label;
    if(current){
      const active=byId.get(current);
      if(active&&(active.parent||active.id)===tab.id){b.classList.add('is-on');b.setAttribute('aria-current','page');}
    }
    b.addEventListener('click',event=>{
      if(event.button||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;
      event.preventDefault();switchTo(tab.defaultView);
    });
    return b;
  }));
}

/* Re-apply the primary navigation after the start (the shell does when
   /api/ui arrives late, or the page list changes). Keeps the current view. */
export function setNavigation(tabDefinitions){
  applyNavigation(tabDefinitions);
  announcePages();
}

export function startRegistry({defaultView,tabs:tabDefinitions}){
  views.sort((a,b)=>(a.order||0)-(b.order||0));
  defaultRoute=defaultView;
  applyNavigation(tabDefinitions);
  if(started)return;
  started=true;
  announcePages();
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
    +'<div style="font:600 11px var(--sans);letter-spacing:.06em;color:var(--ink-3)">VIEW FAILED</div>'
    +'<p style="max-width:44ch;color:var(--ink-2);font-size:14px;margin:10px 0 0">The '+v.label
    +' view hit an error and was isolated. The other tabs keep working. Details are in the browser console.</p>'
    +'</div>';
  el.appendChild(box);
}
