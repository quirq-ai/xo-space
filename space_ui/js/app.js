/* Entry point. Adding a view = create js/views/<name>.js exporting the view
   contract (see core/registry.js), then import + register it here — no
   bundler, so no file globbing; this import list is the one manual step. */
import {registerView,startRegistry} from './core/registry.js?v=20260813-timeline2';
import {initServerWidget} from './core/server-widget.js?v=20260825-rename1';
import {initLensSwitch} from './core/lens-switch.js?v=20260817-lens1';
import {initPreview} from './core/preview.js?v=20260827-dashboard1';
import {graphView,timeView} from './views/atlas.js?v=20260827-dashboard1';
import dashboardView from './views/dashboard.js?v=20260828-clusters1';
import sessionsView from './views/sessions.js?v=20260825-rename1';
import projectsView from './views/projects.js?v=20260827-firstrun1';
import treeView from './views/tree.js?v=20260825-rename1';
/* Chat is deliberately hidden from the tab bar: re-import ./views/chat.js
   and register it below to bring the tab back. */
import wikiView from './views/wiki.js?v=20260828-clusters1';
import quirqView from './views/quirq.js?v=20260817-plural1';
import secretsView from './views/secrets.js?v=20260825-installlink1';
import snapshotView from './views/snapshot.js?v=20260820-snapshot3';

/* app-shell bulkhead: a fatal script error logs instead of white-screening */
addEventListener('error',e=>console.error('Space shell error:',e.error||e.message));
addEventListener('unhandledrejection',e=>console.error('Space unhandled rejection:',e.reason));

/* Before startRegistry: its first switchTo announces the active view, and a
   listener registered afterwards would miss it on a deep link. */
try{initLensSwitch();}catch(err){console.error('Lens switch failed to start:',err);}

try{
  registerView(dashboardView);
  registerView(graphView);
  registerView(timeView);
  registerView(sessionsView);
  registerView(projectsView);
  registerView(treeView);
  registerView(wikiView);
  registerView(quirqView);
  registerView(snapshotView);
  registerView(secretsView);
  startRegistry({defaultView:'dashboard'});
}catch(err){console.error('Space registry failed to start:',err);}

try{initServerWidget();}catch(err){console.error('Server widget failed to start:',err);}
try{initPreview();}catch(err){console.error('Previewer failed to start:',err);}
