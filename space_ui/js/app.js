/* Entry point. Adding a view = create js/views/<name>.js exporting the view
   contract (see core/registry.js), then import + register it here: no
   bundler, so no file globbing; this import list is the one manual step. */
import {registerView,startRegistry,switchTo,refreshCurrentView} from './core/registry.js?v=20260914-actions1';
import {initProjectActions} from './core/project-actions.js?v=20260914-details1';
import {initServerWidget} from './core/server-widget.js?v=20260914-commands2';
import {initToolbar} from './core/toolbar.js?v=20260914-files2';
import {initSectionNav} from './core/section-nav.js?v=20260915-data1';
import {PRIMARY_TABS} from './core/navigation.js?v=20260915-data1';
import {initPreview} from './core/preview.js?v=20260915-data1';
import {dashboardView,graphView,timeView,initProjectRootPicker} from './views/atlas.js?v=20260915-data1';
import {createAgentViews} from './views/sessions.js?v=20260915-data1';
import {createInboxViews,initInboxBadge} from './views/inbox.js?v=20260915-data1';
import {createActivityViews} from './views/inbox-activity.js?v=20260915-data1';
import projectsView from './views/projects.js?v=20260915-data1';
import projectManageView from './views/project-manage.js?v=20260915-data1';
import treeView from './views/tree.js?v=20260915-data1';
import sharingView from './views/sharing.js?v=20260915-data1';
/* Chat is deliberately hidden from the tab bar: re-import ./views/chat.js
   and register it below to bring the tab back. */
import wikiView from './views/wiki.js?v=20260915-data1';
import quirqView from './views/quirq.js?v=20260915-data1';
import {createSetupViews} from './views/setup.js?v=20260914-manage1';
import connectorsView from './views/connectors.js?v=20260914-setupapps1';


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
  startRegistry({tabs:PRIMARY_TABS,defaultView:'projects'});
}catch(err){console.error('Space registry failed to start:',err);}

try{initServerWidget();}catch(err){console.error('Server widget failed to start:',err);}
try{initInboxBadge();}catch(err){console.error('Inbox badge failed to start:',err);}
try{initPreview();}catch(err){console.error('Previewer failed to start:',err);}
