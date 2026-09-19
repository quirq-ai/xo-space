/* Entry point: the shell. Boot fetches GET /api/ui (the tabs and every
   enabled page spec, routers/kernel.py), registers the hand-written views
   below, then one spec view per page (core/spec-view.js) after them, so a
   spec page wins a route the legacy tables also name, and starts the
   registry with the tabs /api/ui named. When /api/ui is unreachable (the
   server is down) the shell boots on navigation.js PRIMARY_TABS and the
   legacy views alone, and fetches /api/ui again the moment the footer's
   server pill sees the server up. A write to /api/modules (the Modules
   page) re-reads /api/ui, so a page whose module switched off leaves
   navigation at once; its route keeps answering with "off in Setup".

   Adding a hand-written view = create js/views/<name>.js exporting the view
   contract (see core/registry.js), then import + register it here: no
   bundler, so no file globbing; this import list is the one manual step. A
   module's page needs no line here: modules/<m>/pages/<page>.json is
   enough. Imports are plain paths with no cache stamps: the /space mount
   sends Cache-Control: no-cache, so a browser revalidates every module on
   load and a changed file is fetched fresh without anyone bumping anything. */
import {registerView,unregisterView,startRegistry,setNavigation,switchTo,refreshCurrentView,listViews} from './core/registry.js';
import {initProjectActions} from './core/project-actions.js';
import {initServerWidget,onServerState} from './core/server-widget.js';
import {initToolbar} from './core/toolbar.js';
import {initSectionNav} from './core/section-nav.js';
import {PRIMARY_TABS} from './core/navigation.js';
import {initPreview} from './core/preview.js';
import {initCommandPalette} from './core/command-palette.js';
import {API_BASE,apiFetch} from './core/api.js';
import {esc} from './core/ui.js';
import * as kit from './core/shadcn.js';
import {specView,MODULES_ROUTE} from './core/spec-view.js';
import {dashboardView,graphView,timeView,initProjectRootPicker} from './views/atlas.js';
import {createAgentViews} from './views/sessions.js';
import {createInboxViews,initInboxBadge} from './views/inbox.js';
import {createActivityViews} from './views/inbox-activity.js';
import projectsView from './views/projects.js';
import projectManageView from './views/project-manage.js';
import treeView from './views/tree.js';
import sharingView from './views/sharing.js';
/* Chat is deliberately hidden from the tab bar: re-import ./views/chat.js
   and register it below to bring the tab back. */
import wikiView from './views/wiki.js';
import quirqView from './views/quirq.js';
import {createSetupViews} from './views/setup.js';
import connectorsView from './views/connectors.js';


/* app-shell bulkhead: a fatal script error logs instead of white-screening */
addEventListener('error',e=>console.error('Space shell error:',e.error||e.message));
addEventListener('unhandledrejection',e=>console.error('Space unhandled rejection:',e.reason));

/* Responsive navigation can occupy several rows. Measure its bottom for
   fixed overlays without changing --topbar-h, which sizes the desktop row. */
function initTopbarInset(){
  const topbar=document.querySelector('.topbar');
  if(!topbar)return;
  let previous=null,previousWidth=null;
  const update=()=>{
    const rect=topbar.getBoundingClientRect();
    const inset=Math.ceil(rect.bottom);
    if(inset<=0)return;
    if(inset!==previous){
      document.documentElement.style.setProperty('--topbar-inset',inset+'px');
      previous=inset;
    }
    if(rect.width!==previousWidth){
      previousWidth=rect.width;
      topbar.querySelector('.tabs .is-on')?.scrollIntoView({block:'nearest',inline:'nearest'});
    }
  };
  update();
  if(typeof ResizeObserver==='function')new ResizeObserver(update).observe(topbar);
  else addEventListener('resize',update);
}
try{initTopbarInset();}catch(err){console.error('Topbar measurement failed:',err);}

/* Before startRegistry: its first switchTo announces the active view, and a
   listener registered afterwards would miss it on a deep link. */
addEventListener('space:view',event=>{
  const link=document.getElementById('wiki-link');
  if(!link)return;
  if(event.detail?.id==='wiki')link.setAttribute('aria-current','page');
  else link.removeAttribute('aria-current');
});
try{initProjectActions(switchTo);}catch(err){console.error('Project actions failed to start:',err);}
try{initProjectRootPicker({switchTo});}catch(err){console.error('Project root picker failed to start:',err);}
try{initToolbar();}catch(err){console.error('Toolbar failed to start:',err);}
try{initSectionNav({refreshCurrentView});}catch(err){console.error('Section navigation failed to start:',err);}

/* ── the page list from /api/ui ─────────────────────────────────────────── */
const specPages=new Map();   /* "<module>/<id>" -> the /api/ui page currently registered */
const offPages=new Set();    /* ids standing in for pages whose module is off */
const retired=new Set();     /* legacy view ids a spec page replaced */

export function tabsFrom(ui){
  return(Array.isArray(ui?.tabs)?ui.tabs:[]).filter(tab=>tab&&tab.id).map(tab=>({
    id:String(tab.id),label:String(tab.label||tab.id),defaultView:String(tab.default||tab.id),
    aliases:[...(tab.aliases||[])].map(String),
  }));
}

async function fetchUi(){
  const res=await apiFetch(API_BASE+'/api/ui');
  if(!res.ok||!res.data||!Array.isArray(res.data.pages)||!Array.isArray(res.data.tabs))return null;
  return res.data;
}

/* A route whose page switched off still answers: it says why, and where. */
function offView(page){
  return{
    id:page.module+'/'+page.id,route:page.route,aliases:[...(page.aliases||[])],label:page.label,
    order:page.order||0,nav:false,parent:page.tab,section:'spec-'+page.module+'-'+page.id,secondary:false,
    async mount(el){
      el.classList.add('spec-view');
      el.innerHTML='<div class="spec-page"><header class="spec-head"><h1>'+esc(page.label)+'</h1></header>'
        +kit.alert({icon:kit.icons.alert,title:esc(page.label)+' is off in Setup',
          description:'Its module is switched off, so this page left navigation. <a href="#/'+MODULES_ROUTE+'">Open Setup, Modules</a> to turn it on.'})
        +'</div>';
    },
  };
}

/* Bring the registry in line with one /api/ui answer: spec pages that
   arrived are registered, pages that left are replaced by an "off" note,
   legacy views on routes the specs own are retired. Returns the tabs. */
function applyUi(ui){
  const wanted=new Map();
  for(const page of ui.pages){
    if(!page||!page.id||!page.module||!page.route||!page.tab)continue;
    wanted.set(page.module+'/'+page.id,page);
  }
  for(const[id,page]of wanted){
    if(specPages.has(id))continue;
    if(offPages.has(id)){unregisterView(id);offPages.delete(id);}
    registerView(specView(page));
    specPages.set(id,page);
  }
  for(const[id,page]of[...specPages]){
    if(wanted.has(id))continue;
    unregisterView(id);specPages.delete(id);
    registerView(offView(page));offPages.add(id);
  }
  const routes=new Set();
  for(const page of wanted.values()){routes.add(page.route);for(const alias of page.aliases||[])routes.add(alias);}
  for(const view of listViews()){
    if(view.spec||specPages.has(view.id)||offPages.has(view.id))continue;
    if(routes.has(view.route)||view.aliases.some(alias=>routes.has(alias))){unregisterView(view.id);retired.add(view.id);}
  }
  return tabsFrom(ui);
}

let syncing=null;
function syncUi(){
  if(syncing)return syncing;
  syncing=(async()=>{
    const ui=await fetchUi();
    if(!ui)return false;
    setNavigation(applyUi(ui));
    return true;
  })().catch(err=>{console.error('Space could not re-read /api/ui:',err);return false;}).finally(()=>{syncing=null;});
  return syncing;
}

/* The server was down at boot: try again when the footer pill sees it up. */
function retryUiWhenUp(){
  let stop=()=>{};
  stop=onServerState(async up=>{
    if(!up)return;
    if(await syncUi())stop();
  });
}

let ui=null;
try{ui=await fetchUi();}catch(err){console.error('Space could not read /api/ui:',err);}

try{
  registerView(dashboardView);
  registerView(graphView);
  registerView(timeView);
  createAgentViews().forEach(registerView);
  createInboxViews().forEach(registerView);
  createActivityViews().forEach(registerView);
  registerView(projectsView);
  registerView(projectManageView);
  registerView(treeView);
  registerView(sharingView);
  registerView(wikiView);
  registerView(quirqView);
  createSetupViews(connectorsView).forEach(registerView);
  if(ui){
    const tabs=applyUi(ui);
    startRegistry({tabs:tabs.length?tabs:PRIMARY_TABS,defaultView:'projects'});
  }else{
    startRegistry({tabs:PRIMARY_TABS,defaultView:'projects'});
    retryUiWhenUp();
  }
}catch(err){console.error('Space registry failed to start:',err);}

/* A switch flipped on the Modules page: pages come and go, so re-read the
   list the kernel now serves. */
addEventListener('space:wrote',event=>{
  if(String(event.detail?.path||'').startsWith('/api/modules'))syncUi();
});

try{initServerWidget();}catch(err){console.error('Server widget failed to start:',err);}
try{initInboxBadge();}catch(err){console.error('Inbox badge failed to start:',err);}
try{initPreview();}catch(err){console.error('Previewer failed to start:',err);}
try{initCommandPalette({switchTo,refreshCurrentView});}catch(err){console.error('Command palette failed to start:',err);}
