"""Atlas projection requests cannot reload or reclaim another UI page."""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class AtlasLifecycleTests(unittest.TestCase):
    def test_projection_lifecycle_and_project_change_invalidation(self) -> None:
        probe = r'''
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const events=new Map(),elements=new Map(),reads=[],boots=[],activations=[];
let disposed=0,reloads=0,reply={ok:true,data:{version:1}},pending=null;
function element(id=''){
  return {id,hidden:false,value:'',children:[],listeners:new Map(),
    classList:{remove(){}},setAttribute(){},remove(){},
    querySelector(selector){
      if(selector==='.atlas-project-refresh')return this.children.find(child=>child.className==='atlas-project-refresh');
      this.button??=element('button');return this.button;
    },
    querySelectorAll(){return[];},
    addEventListener(type,handler){this.listeners.set(type,handler);},
    appendChild(child){this.children.push(child);},
  };
}
const context={
  AbortController,setTimeout,clearTimeout,
  API_BASE:'',projectPage:id=>({id}),toast(){},
  addEventListener:(type,handler)=>events.set(type,handler),
  document:{getElementById:id=>{if(!elements.has(id))elements.set(id,element(id));return elements.get(id);},
    querySelectorAll:()=>[],createElement:()=>element()},
  localStorage:{getItem(){return null;},setItem(){}},
  location:{reload(){reloads++;}},
  apiFetch:async url=>{reads.push(url);return pending?await pending.promise:reply;},
  recordBoot:(data,source)=>boots.push([data.version,source]),
  recordActive:view=>activations.push(view),
  recordDispose:()=>disposed++,
};
vm.createContext(context);
let source=fs.readFileSync('space_ui/js/views/atlas.js','utf8')
  .replace(/^import .*?;\n/gm,'').replaceAll('export const ','const ').replaceAll('export function ','function ');
// The lifecycle under test is real. The canvas engine is represented by its
// public hooks; actual engine interactions are exercised in the browser suite.
source=source.slice(0,source.indexOf('function boot(DATA,DATA_SOURCE,bootDataset){'))+`
function boot(data,source){
  recordBoot(data,source);
  hooks.setActiveView=recordActive;
  hooks.dispose=recordDispose;
}
globalThis.pages={dashboard:dashboardView,graph:graphView,time:timeView};
`;
vm.runInContext(source,context);
const announce=id=>events.get('space:view')({detail:{id}});
async function open(id){
  announce(id);
  const page=context.pages[id];
  await page.mount(element(),{switchTo(){},refreshToolbar(){}});
  await page.show();
}
function gate(){let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};}
await open('dashboard');
assert.deepEqual(boots,[[1,'Overview']]);
await open('graph');await open('time');await open('dashboard');
assert.equal(reloads,0,'ordinary projection navigation never reloads the application');
assert.deepEqual(reads,['/xo/dashboard.json','/xo/space.json'],'visited projections reuse their data snapshot');
assert.deepEqual(boots,[[1,'Overview'],[1,'Graph'],[1,'Overview']]);
assert.equal(disposed,2,'replaced engines dispose their listeners and animation work');
// Both cached datasets must be invalidated when a project changes.
events.get('space:projects-changed')({});reply={ok:true,data:{version:2}};
await open('graph');
assert.deepEqual(boots.at(-1),[2,'Graph']);
assert.equal(reads.filter(url=>url==='/xo/space.json').length,2);
// A refresh completed after leaving the map must not activate hidden hooks.
events.get('space:projects-changed')({});
pending=gate();
const notice=elements.get('view-graph').children.at(-1);
const refreshing=notice.button.listeners.get('click')();
announce('setup/workspace');
const count=activations.length;
pending.resolve({ok:true,data:{version:3}});await refreshing;pending=null;
assert.equal(activations.length,count,'late refresh cannot activate the hidden map');
// A slow first read for another projection cannot replace a newer page.
events.get('space:projects-changed')({});
pending=gate();announce('dashboard');
const loading=context.pages.dashboard.show();
announce('project-list');
const bootCount=boots.length;
pending.resolve({ok:true,data:{version:4}});await loading;pending=null;
assert.equal(boots.length,bootCount,'late projection response cannot rebuild another page');
assert.equal(reloads,0);
'''
        result = subprocess.run(
            ['node', '--input-type=module', '-e', probe], cwd=ROOT,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
