"""Refresh and project-form handoffs use the completed, current activation.

Run the real modules in Node with deferred lifecycle hooks. No services,
browser, file operations, or project mutations are involved.
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRELUDE = r"""
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';

const emitted=[],elements=new Map(),listeners=new Map(),logged=[];
globalThis.location={hash:''};
globalThis.history={
  pushState:(_state,_title,hash)=>{location.hash=hash;},
  replaceState:(_state,_title,hash)=>{location.hash=hash;},
};
globalThis.CustomEvent=class{
  constructor(type,options={}){this.type=type;this.detail=options.detail;}
};
globalThis.addEventListener=(type,handler)=>{
  if(!listeners.has(type))listeners.set(type,[]);
  listeners.get(type).push(handler);
};
globalThis.dispatchEvent=event=>{
  emitted.push(event);
  for(const handler of listeners.get(event.type)||[])handler(event);
};
globalThis.requestAnimationFrame=handler=>queueMicrotask(handler);
console.error=(...args)=>logged.push(args);
class Element{
  constructor(id=''){
    this.id=id;this.children=[];this.style={};this.attributes={};
    const classes=new Set();
    this.classList={contains:name=>classes.has(name),
      toggle:(name,on)=>on?classes.add(name):classes.delete(name)};
    if(id)elements.set(id,this);
  }
  appendChild(child){this.children.push(child);return child;}
  setAttribute(name,value){this.attributes[name]=value;}
  removeAttribute(name){delete this.attributes[name];}
  scrollIntoView(){}
}
new Element('stage');
globalThis.document={
  getElementById:id=>elements.get(id)||null,
  querySelectorAll:()=>[],
  createElement:()=>new Element(),
};
const base=pathToFileURL(process.cwd()+'/space_ui/js/core/');
const registry=await import(new URL('registry.js',base));
const actions=await import(new URL('project-actions.js',base));
const register=spec=>{
  const view={label:spec.id,mount:async()=>{},...spec};
  if(!elements.has('view-'+(view.section||view.id)))new Element('view-'+(view.section||view.id));
  registry.registerView(view);return view;
};
const gate=()=>{
  let resolve,reject;
  const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});
  return{promise,resolve,reject};
};
const settle=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
const states=id=>emitted.filter(event=>event.type==='space:refresh-state'&&event.detail.id===id).map(event=>event.detail.busy);
const viewEvent=()=>emitted.filter(event=>event.type==='space:view').at(-1)?.detail;
const formEvents=()=>emitted.filter(event=>event.type==='space:add-project');
"""


@unittest.skipUnless(shutil.which("node"), "node is unavailable")
class ProjectActionTests(unittest.TestCase):
    def probe(self, source: str) -> None:
        result = subprocess.run(
            ["node", "--input-type=module", "-e", PRELUDE + source],
            cwd=ROOT, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_switch_result_rejects_missing_failed_and_stale_activations(self):
        self.probe(r"""
const mount=gate();let mounts=0,shows=0;
register({id:'slow',route:'projects/files/list',mount:()=>{mounts++;return mount.promise;},show:()=>{shows++;}});
register({id:'other',route:'inbox/items'});
assert.equal(await registry.switchTo('missing'),false);
const first=registry.switchTo('slow');
assert.equal(location.hash,'#/projects/files/list');
assert.equal(await registry.switchTo('other'),true);
const latest=registry.switchTo('slow');
mount.resolve();
assert.deepEqual(await Promise.all([first,latest]),[false,true]);
assert.equal(mounts,1,'Reentry shares the pending mount');
assert.equal(shows,1,'Only the latest activation may show the view');
register({id:'failed',mount:async()=>{throw new Error('fixture mount failure');},
  refresh:()=>assert.fail('A failed mount cannot be refreshed')});
assert.equal(await registry.switchTo('failed'),false);
assert.equal(await registry.switchTo('failed'),false,'A failed mount cannot later report a successful activation');
await registry.refreshCurrentView();assert.deepEqual(states('failed'),[true,false]);
assert.equal(await registry.switchTo('other'),true,'Mount failure is isolated');
""")

    def test_slow_show_completion_cannot_report_a_newer_activation_as_its_own(self):
        self.probe(r"""
const shown=gate();let entered=0;
register({id:'slow-show',show:()=>{entered++;return shown.promise;}});
register({id:'other'});
const pending=registry.switchTo('slow-show');await settle();
assert.equal(entered,1);
assert.equal(await registry.switchTo('other'),true);
shown.resolve();
assert.equal(await pending,false);
assert.equal(location.hash,'#/other');
register({id:'failed-show',show:async()=>{throw new Error('fixture show failure');}});
assert.equal(await registry.switchTo('failed-show'),false);
""")

    def test_refresh_waits_for_mount_and_cancels_after_activation_changes(self):
        self.probe(r"""
const mount=gate();let refreshes=0;
register({id:'slow',mount:()=>mount.promise,refresh:()=>{refreshes++;}});
register({id:'other'});
const first=registry.switchTo('slow');
const requested=registry.refreshCurrentView();
await settle();assert.equal(refreshes,0,'Refresh must not run against an unmounted view');
await registry.switchTo('other');
const latest=registry.switchTo('slow');
mount.resolve();
await Promise.all([first,latest,requested]);
assert.equal(refreshes,0,'Leaving and reentering the same view invalidates the older refresh request');
await registry.refreshCurrentView();
assert.equal(refreshes,1,'A new explicit refresh still works');
assert.deepEqual(states('slow').slice(-2),[true,false]);
""")

    def test_refresh_deduplicates_requests_before_and_after_mount(self):
        self.probe(r"""
const mounted=gate(),finished=gate();let refreshes=0;
register({id:'list',mount:()=>mounted.promise,refresh:()=>{refreshes++;return finished.promise;}});
const activation=registry.switchTo('list');
assert.equal(viewEvent().refreshable,true);assert.equal(viewEvent().refreshing,false);
const first=registry.refreshCurrentView(),second=registry.refreshCurrentView();
mounted.resolve();await activation;await settle();
assert.equal(refreshes,1,'Concurrent requests waiting on the same mount share one refresh');
const third=registry.refreshCurrentView();await settle();
assert.equal(refreshes,1,'Requests during the refresh share its work');
assert.deepEqual(states('list'),[true]);
await registry.switchTo('list');
assert.equal(viewEvent().refreshable,true);assert.equal(viewEvent().refreshing,true);
finished.resolve();await Promise.all([first,second,third]);
assert.deepEqual(states('list'),[true,false]);
await registry.switchTo('list');assert.equal(viewEvent().refreshing,false);
""")

    def test_refresh_failure_clears_busy_and_allows_retry(self):
        self.probe(r"""
let refreshes=0;
register({id:'list',refresh:()=>{
  refreshes++;
  if(refreshes===1)return Promise.reject(new Error('fixture rejected refresh'));
  if(refreshes===2)throw new Error('fixture synchronous refresh failure');
}});
await registry.switchTo('list');
for(let i=0;i<2;i++){
  await Promise.allSettled([registry.refreshCurrentView()]);
  assert.deepEqual(states('list').slice(-2),[true,false]);
  await registry.switchTo('list');assert.equal(viewEvent().refreshing,false);
}
await registry.refreshCurrentView();assert.equal(refreshes,3);
assert.deepEqual(states('list'),[true,false,true,false,true,false]);
""")

    def test_pending_refresh_state_stays_attached_to_its_view(self):
        self.probe(r"""
const finished=gate();let refreshes=0;
register({id:'list',refresh:()=>{refreshes++;return finished.promise;}});
register({id:'wiki'});
await registry.switchTo('list');const pending=registry.refreshCurrentView();await settle();
await registry.switchTo('wiki');
assert.equal(viewEvent().refreshable,false);assert.equal(viewEvent().refreshing,false);
await registry.refreshCurrentView();assert.equal(refreshes,1);
assert.deepEqual(states('wiki'),[],'A page without refresh does not emit busy events');
await registry.switchTo('list');assert.equal(viewEvent().refreshing,true);
const again=registry.refreshCurrentView();await settle();assert.equal(refreshes,1);
finished.resolve();await Promise.all([pending,again]);
assert.deepEqual(states('list'),[true,false]);
""")

    def test_legacy_setup_project_event_routes_without_mounting_setup_or_opening_add(self):
        self.probe(r"""
const targets=[];
actions.initProjectActions(route=>targets.push(route));
assert.equal(elements.has('view-setup'),false,'Setup has not mounted');
dispatchEvent(new CustomEvent('space:setup-section',{detail:{panel:'projects'}}));
assert.deepEqual(targets,['projects/manage']);
assert.deepEqual(formEvents(),[],'The old management event must not open Add');
for(const detail of [{panel:'workspace'},{panel:'activity'},{panel:'project'},null])
  dispatchEvent(new CustomEvent('space:setup-section',{detail}));
assert.deepEqual(targets,['projects/manage'],'Other Setup events stay with Setup');
""")

    def test_handoffs_require_successful_navigation_and_the_canonical_route(self):
        self.probe(r"""
for(const [run,route,type] of [
  [navigate=>actions.openProjectAdd(navigate),'projects/manage','space:add-project'],
]){
  for(const [completed,hash,allowed] of [
    [true,'#/'+route,true],[false,'#/'+route,false],[undefined,'#/'+route,false],[1,'#/'+route,false],
    [true,'#/inbox/items',false],[true,'#/setup/projects',false],
  ]){
    emitted.length=0;
    await run(async target=>{assert.equal(target,route);location.hash=hash;return completed;});
    assert.equal(formEvents().length,allowed?1:0);
    if(allowed)assert.equal(formEvents()[0].type,type);
  }
}
""")

    def test_form_handoffs_do_not_fire_from_stale_slow_navigation(self):
        self.probe(r"""
register({id:'other',route:'inbox/items'});
for(const [id,route,run] of [
  ['project-manage','projects/manage',()=>actions.openProjectAdd(registry.switchTo)],
]){
  const mounted=gate();register({id,route,mount:()=>mounted.promise});
  emitted.length=0;
  const handoff=run();await settle();
  await registry.switchTo('other');
  // Same URL is insufficient: a second activation superseded this request.
  const current=registry.switchTo(route);
  mounted.resolve();await Promise.all([handoff,current]);
  assert.equal(location.hash,'#/'+route);assert.deepEqual(formEvents(),[]);
  await run();assert.equal(formEvents().length,1,'A fresh completed handoff opens the form');
}
""")

    def test_activity_handoff_waits_for_current_activation_and_keeps_project_payload(self):
        self.probe(r"""
const activityEvents=()=>emitted.filter(event=>event.type==='space:activity-project');
for(const [completed,hash,allowed] of [
  [true,'#/inbox/activity',true],[false,'#/inbox/activity',false],
  [undefined,'#/inbox/activity',false],[true,'#/projects/files/list',false],
]){
  emitted.length=0;
  await actions.openProjectActivity(async route=>{
    assert.equal(route,'inbox/activity');location.hash=hash;return completed;
  },'alpha-project');
  assert.equal(activityEvents().length,allowed?1:0);
  if(allowed)assert.deepEqual(activityEvents()[0].detail,{project_id:'alpha-project'});
}
const mounted=gate();register({id:'inbox-activity',route:'inbox/activity',mount:()=>mounted.promise});
register({id:'other',route:'projects/files/list'});emitted.length=0;
const old=actions.openProjectActivity(registry.switchTo,'alpha-project');await settle();
await registry.switchTo('other');const latest=registry.switchTo('inbox/activity');
mounted.resolve();await Promise.all([old,latest]);
assert.deepEqual(activityEvents(),[],'Same URL does not revive an older handoff');
await actions.openProjectActivity(registry.switchTo,'beta-project');
assert.deepEqual(activityEvents()[0].detail,{project_id:'beta-project'});
""")


if __name__ == "__main__":
    unittest.main()
