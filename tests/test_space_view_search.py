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

function module(name,extra={}){
  const events=new Map(),timers=new Map();let serial=0;
  const context={
    API_BASE:'',addEventListener:(type,fn)=>events.set(type,fn),
    document:{getElementById:()=>null},CSS:{escape:value=>value},
    setTimeout:fn=>{timers.set(++serial,fn);return serial;},
    clearTimeout:id=>timers.delete(id),...extra,
  };
  vm.createContext(context);
  const source=fs.readFileSync(process.argv[1]+'/space_ui/js/views/'+name+'.js','utf8')
    .replace(/^import .*?;\n/gm,'').replace('export default','globalThis.view =');
  vm.runInContext(source,context);
  return{view:context.view,events,flush:()=>{
    const pending=[...timers.values()];timers.clear();pending.forEach(fn=>fn());
  }};
}

// Projects: debounced matching, retained query, and programmatic handoff
// to an already-expanded project that the current filter has hidden.
const body={innerHTML:''},count={textContent:''};
let html='',failList=false,refresh,toolbarRefreshes=0;
const box={get innerHTML(){return html;},set innerHTML(value){html=value;body.innerHTML=value;}};
const button={disabled:false,classList:{add(){}},addEventListener:(_type,fn)=>{refresh=fn;}};
const projectsRoot={innerHTML:'',
  querySelector(selector){
    if(selector==='.prj')return box;
    if(selector==='.prj-body')return html.includes('class="prj-body"')?body:null;
    if(selector==='#prj-count')return count;
    if(selector==='#prj-refresh')return html.includes('id="prj-refresh"')?button:null;
    return null;
  },querySelectorAll:()=>[],
};
const projects=module('projects',{
  workspaceCounts:async()=>({byProject:new Map()}),
  apiFetch:async path=>path==='/api/xo-projects'
    ? failList?{ok:false,error:'Catalog unavailable'}:{ok:true,data:{items:[
      {id:'alpha',display_name:'Aurora'}, {id:'beta',display_name:'Beacon'},
    ]}}
    :{ok:false},
});
const ps=projects.view.toolbar.search;
ps.setValue('AUR');projects.flush(); // safe before mount/data
await projects.view.mount(projectsRoot,{switchTo(){},refreshToolbar(){toolbarRefreshes++;}});
assert.match(html,/prj-row-alpha/);assert.doesNotMatch(html,/prj-row-beta/);
assert.doesNotMatch(html,/id="prj-filter"/);
ps.setValue('alpha');ps.setValue(' beta ');
assert.match(body.innerHTML,/prj-row-alpha/,'results wait for debounce');
projects.flush();
assert.match(body.innerHTML,/prj-row-beta/);assert.doesNotMatch(body.innerHTML,/prj-row-alpha/);
projects.view.show();assert.equal(ps.getValue(),' beta ');
const jump=id=>projects.events.get('space:open-project')({detail:id});
jump('beta');
ps.setValue('alpha');projects.flush();
assert.doesNotMatch(body.innerHTML,/prj-row-beta/);
jump('beta');
assert.equal(ps.getValue(),'');assert.equal(toolbarRefreshes,1);
assert.match(html,/prj-drawer-beta/,'handoff reveals the existing expanded drawer');
ps.setValue('missing');projects.flush();assert.match(body.innerHTML,/No project matches/);
failList=true;await refresh();
ps.setValue('alpha');projects.flush();
assert.match(html,/Catalog unavailable/,'search preserves request failure feedback');

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
    def test_projects_and_tree_preserve_state_and_loading_feedback(self) -> None:
        result = subprocess.run(
            ["node", "--input-type=module", "-e", LOCAL_PROBE, str(ROOT)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_connectors_search_preserves_pending_actions_and_polling_drafts(self) -> None:
        self.assertTrue(run_node(CONNECTORS_PROBE)["passed"])
