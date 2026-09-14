"""Exercise contextual view searches against the real view modules.

The shell owns the input; these checks cover each view's result and state
contract, including asynchronous Connectors actions while cards are hidden.
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

from tests.test_space_pr97_ui import PROBE_PRELUDE, run_node


ROOT = Path(__file__).resolve().parents[1]

LOCAL_PROBE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const {projectPage}=await import(pathToFileURL(process.argv[1]+'/space_ui/js/core/navigation.js'));
const {pageHeader}=await import(pathToFileURL(process.argv[1]+'/space_ui/js/core/page-layout.js'));

function module(name,extra={}){
  const events=new Map(),timers=new Map();let serial=0;
  const context={
    API_BASE:'',projectPage,pageHeader,location:{origin:'http://space.test'},addEventListener:(type,fn)=>events.set(type,fn),
    document:{getElementById:()=>null},CSS:{escape:value=>value},
    setTimeout:fn=>{timers.set(++serial,fn);return serial;},
    clearTimeout:id=>timers.delete(id),...extra,
  };
  vm.createContext(context);
  const source=fs.readFileSync(process.argv[1]+'/space_ui/js/views/'+name+'.js','utf8')
    .replace(/^import .*?;\n/gm,'').replace('export default','globalThis.view =');
  vm.runInContext(source,context);
  return{view:context.view,events,evaluate:source=>vm.runInContext(source,context),flush:()=>{
    const pending=[...timers.values()];timers.clear();pending.forEach(fn=>fn());
  }};
}

// Projects: exercise the real selectors and pin actions without pretending
// innerHTML is a DOM. The browser projects-experience harness covers keyed
// nodes, focus, request failures, debounce rendering and project handoffs.
const storage=new Map([['space.projects.pins.v1:http://space.test','["beta"]']]);
const projects=module('projects',{
  localStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)},
});
const ps=projects.view.toolbar.search;
ps.setValue('AUR');projects.flush(); // safe before mount/data
assert.deepEqual(Array.from(projects.evaluate('visible()')),[]);
projects.evaluate(`
  items=[
    {id:'alpha',display_name:'Aurora',description:'Finance reporting workspace',created_at:'2026-01-01T00:00:00Z'},
    {id:'beta',display_name:'Beacon',description:'Team conversation and inbox',created_at:'2026-03-01T00:00:00Z'},
    {id:'gamma',display_name:'Aurora',description:'Finance forecast tools',created_at:'2026-02-01T00:00:00Z'},
    {id:'delta',display_name:'Delta',description:null,created_at:null},
  ];
  counts=new Map([
    ['alpha',{files:0,known:true}],['beta',{files:30,known:true}],
    ['gamma',{files:9999,known:false}],
  ]);
  live=new Map([
    ['beta',{since:'2026-05-01T00:00:00Z'}],
    ['gamma',{since:'2026-06-01T00:00:00Z'}],
  ]);
  lastEvent=new Map([['alpha','2026-07-01T00:00:00Z']]);
  feeds.activity='ready';sortK='name';
`);
const projectIds=()=>Array.from(projects.evaluate('visible().map(p=>p.id)'));
assert.deepEqual(projectIds(),['alpha','gamma'],'query entered before data remains active');
ps.setValue('  FINANCE   aur  ');projects.flush();
assert.deepEqual(projectIds(),['alpha','gamma'],'all words may match across name and description');
ps.setValue('alpha reporting');projects.flush();
assert.deepEqual(projectIds(),['alpha'],'project ID and description are searchable together');
ps.setValue('finance missing');projects.flush();assert.deepEqual(projectIds(),[]);
ps.setValue('inbox');projects.flush();assert.deepEqual(projectIds(),['beta'],'description-only match');
assert.equal(ps.getValue(),'inbox');
ps.setValue('');projects.flush();
assert.deepEqual(projectIds(),['alpha','gamma','beta','delta'],'name ties use stable project IDs');
projects.evaluate("sortK='files'");
assert.deepEqual(projectIds(),['beta','alpha','gamma','delta'],'unknown count sorts after genuine zero');
projects.evaluate("sortK='created'");
assert.deepEqual(projectIds(),['beta','gamma','alpha','delta'],'newest first, missing date last');
projects.evaluate("sortK='activity'");
assert.deepEqual(projectIds(),['gamma','beta','alpha','delta'],'live projects precede recent idle activity');
projects.evaluate("items[3].created_at='2099-01-01T00:00:00Z';items[3].unscaffolded=true");
assert.deepEqual(projectIds(),['gamma','beta','alpha','delta'],'folder mtime is not recorded activity');
projects.evaluate("sortK='created'");
assert.deepEqual(projectIds(),['beta','gamma','alpha','delta'],'unscaffolded folder mtime is not project creation');
projects.evaluate("viewFilter='live';sortK='name'");
assert.deepEqual(projectIds(),['gamma','beta']);
ps.setValue('finance');projects.flush();assert.deepEqual(projectIds(),['gamma'],'text and live filters intersect');
projects.evaluate("viewFilter='pinned'");
assert.deepEqual(projectIds(),[],'pins do not bypass the current search');
ps.setValue('');projects.flush();assert.deepEqual(projectIds(),['beta'],'stored browser pins restore');
projects.evaluate(`
  globalThis.pinActions=new Map();
  bindRow({hidden:false,querySelector:selector=>({addEventListener:(_type,fn)=>pinActions.set(selector,fn)})},'alpha');
  pinActions.get('.prj-pin')();
`);
assert.deepEqual(projectIds(),['alpha','beta'],'pin action immediately updates the filtered list');
assert.deepEqual(JSON.parse(storage.get('space.projects.pins.v1:http://space.test')),['beta','alpha']);
projects.evaluate("pinActions.get('.prj-pin')()");
assert.deepEqual(projectIds(),['beta'],'unpin removes a project from the pinned filter');
assert.deepEqual(JSON.parse(storage.get('space.projects.pins.v1:http://space.test')),['beta']);
projects.evaluate("viewFilter='all'");
assert.deepEqual(projectIds(),['alpha','gamma','beta','delta'],'clearing the filter restores the full catalog');

// Tree: search while loading, ancestor/name matching, clear restoring the
// expansion and camera, and no focus-stealing replacement input.
let release;const gate=new Promise(resolve=>{release=resolve;});
const treeBox={innerHTML:''},canvas={clientWidth:1200,getBoundingClientRect:()=>({left:0,top:0})};
const surface={style:{},classList:{remove(){}}},treeEvents=new Map();
const treeRoot={
  set innerHTML(value){treeBox.innerHTML=value;},
  addEventListener:(type,fn)=>treeEvents.set(type,fn),
  querySelector:selector=>selector==='.tv'?treeBox:selector==='.tv-canvas'?canvas
    :selector==='.tv-surface'?surface:null,
};
const tree=module('tree',{apiFetch:async()=>{
  await gate;return{ok:true,data:{root:{label:'Workspace'},
    hubs:[{id:'p_alpha',label:'Aurora'},{id:'p_beta',label:'Beacon'}],
    leaves:[{path:'alpha/src/readme.md',label:'readme.md'},{path:'beta/main.py',label:'main.py'}],
  }};
}});
const ts=tree.view.toolbar.search;
const mounted=tree.view.mount(treeRoot,{switchTo(){}});
ts.setValue('README');tree.flush();
assert.match(treeBox.innerHTML,/loading the workspace/);
release();await mounted;
assert.match(treeBox.innerHTML,/readme.md/);assert.doesNotMatch(treeBox.innerHTML,/main.py/);
assert.doesNotMatch(treeBox.innerHTML,/id="tv-filter"/);
ts.setValue('');tree.flush();
const open=key=>treeEvents.get('click')({target:{closest:selector=>
  selector==='[data-key]'?{dataset:{key}}:null}});
open('/alpha');open('/alpha/src');
const camera=surface.style.transform;
assert.match(treeBox.innerHTML,/readme.md/);
ts.setValue('nothing <matches>');tree.flush();
assert.match(treeBox.innerHTML,/No names match “nothing &lt;matches&gt;”/);
tree.view.show();assert.equal(ts.getValue(),'nothing <matches>');
ts.setValue('');tree.flush();
assert.match(treeBox.innerHTML,/readme.md/,'clearing restores expanded branches');
assert.equal(surface.style.transform,camera);
"""

CONNECTORS_PROBE = PROBE_PRELUDE + r"""
const assert=(await import('node:assert/strict')).default;
TOOLKITS[0].description='Read your messages and inbox';
TOOLKITS[1].description='Team conversation';
TOOLKITS.push({id:'telegram',slug:'telegram',display_name:'Telegram',status:'',schemes:['API_KEY']});
accounts={gmail:'dev@example.com'};
let releaseAccount;accountGate=new Promise(r=>{releaseAccount=r;});
accountReply=id=>({toolkit:id,account_label:'ops@example.com'});
const search=view.toolbar.search;
search.setValue('inbox'); // safe before mount; query applied after loading
await view.mount(root);await settle();
const visible=()=>root.querySelectorAll('.conn-card[data-toolkit]').filter(c=>!c.hidden).map(c=>c.dataset.toolkit);
assert.deepEqual(visible(),['gmail']);
search.setValue('  TELEGRAM  ');assert.deepEqual(visible(),['telegram']);
search.setValue('dev@');assert.deepEqual(visible(),['gmail']);
search.setValue('ops@');assert.deepEqual(visible(),[]);assert.equal(noMatch.hidden,false);
const beforeAccount=paints.length;
releaseAccount();accountGate=null;await settle();
assert.deepEqual(visible(),['slack']);assert.equal(noMatch.hidden,true);
assert.equal(paints.length,beforeAccount,'late account matching preserves cards');

search.setValue('');
click('gmail','polling');await settle();
gmail().interval='1800';gmail().collectors.cal=true;
click('slack','actions');await settle();
const draft=gmail();
let releasePoll;pollGate=new Promise(r=>{releasePoll=r;});
const pollButton=click('gmail','poll-now');await settle();
let releasePrefs;prefsGate=new Promise(r=>{releasePrefs=r;});
const actionInput={dataset:{slug:'send'},checked:false,disabled:false,
  closest:()=>({dataset:{toolkit:'slack'}})};
grid.listeners.forEach(fn=>fn({target:{closest:selector=>selector.startsWith('input')?actionInput:null}}));
let releaseConnect;connectGate=new Promise(r=>{releaseConnect=r;});
globalThis.window={open:()=>({close(){}})};
const connectButton=click('telegram','connect');await settle();
const beforeFilter=paints.length,originalCards=[...cardNodes.values()];
search.setValue('missing <connector>');
assert.deepEqual(visible(),[]);assert.equal(noMatch.hidden,false);
assert.match(noMatch.textContent,/missing <connector>/);
search.setValue('');view.show();
assert.equal(search.getValue(),'');assert.equal(noMatch.hidden,true);
assert.equal(paints.length,beforeFilter,'typing never rebuilds busy cards');
assert.deepEqual([...cardNodes.values()],originalCards);
assert.equal(gmail(),draft);assert.equal(gmail().interval,'1800');assert.equal(gmail().collectors.cal,true);
assert.equal(pollButton.disabled,true);assert.equal(pollButton.detached,false);
assert.equal(connectButton.disabled,true);assert.equal(connectButton.detached,false);
assert.equal(actionInput.disabled,true);assert.equal(actionInput.checked,false);
releaseConnect();connectGate=null;releasePrefs();prefsGate=null;await settle();
assert.equal(connectButton.disabled,false);assert.equal(actionInput.disabled,false);
releasePoll();pollGate=null;await settle();
assert.equal(pollButton.disabled,false);

// Search must not mistake a server-side empty catalog for no query matches.
TOOLKITS.length=0;refresh();await settle();
search.setValue('gmail');
assert.match(grid._html,/No toolkits are registered/);assert.equal(noMatch.hidden,true);
console.log(JSON.stringify({passed:true}));process.exit(0);
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SpaceViewSearchTests(unittest.TestCase):
    def test_projects_search_filters_sort_pins_and_tree_state(self) -> None:
        result = subprocess.run(
            ["node", "--input-type=module", "-e", LOCAL_PROBE, str(ROOT)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_connectors_search_preserves_pending_actions_and_polling_drafts(self) -> None:
        self.assertTrue(run_node(CONNECTORS_PROBE)["passed"])
