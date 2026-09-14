"""Exercise the real view metadata, registry and lens switch under Node.

View mounting is stubbed: this tests shell routing without fetching project
data or simulating a canvas. The browser preview harness covers rendered views.
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROBE = r"""
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';

const base=pathToFileURL(process.cwd()+'/space_ui/');
const events=new Map(),elements=new Map();
globalThis.location={pathname:'/space/',search:'',hash:process.argv[1]};
globalThis.localStorage={getItem:()=>null};
globalThis.addEventListener=(type,fn)=>{
  const listeners=events.get(type)||[];listeners.push(fn);events.set(type,listeners);
};
globalThis.dispatchEvent=e=>{for(const fn of events.get(e.type)||[])fn(e);};
globalThis.CustomEvent=class{constructor(type,options={}){this.type=type;this.detail=options.detail;}};
globalThis.history={replaceState:(_state,_title,hash)=>{location.hash=hash;}};
globalThis.requestAnimationFrame=fn=>fn();
class Element{
  constructor(){
    this.dataset={};this.attributes={};this.listeners={};this.classes=new Set();
    this.classList={toggle:(name,on)=>on?this.classes.add(name):this.classes.delete(name),
      contains:name=>this.classes.has(name)};
  }
  addEventListener(type,fn){this.listeners[type]=fn;}
  setAttribute(name,value){this.attributes[name]=value;}
  scrollIntoView(){}
  appendChild(child){elements.set(child.id,child);}
  replaceChildren(...children){this.children=children;children.forEach(child=>this.appendChild(child));}
}
const stage=new Element(),tabs=new Element(),pill=new Element();
elements.set('stage',stage);elements.set('fileslens',pill);
const html=await readFile(new URL('index.html',base),'utf8');
const buttons=[...html.matchAll(/data-files-lens="([^"]+)"[^>]*>([^<]+)</g)].map(([,id,label])=>{
  const button=new Element();button.dataset.filesLens=id;button.label=label;
  button.closest=()=>button;return button;
});
pill.querySelectorAll=()=>buttons;
globalThis.document={activeElement:null,getElementById:id=>elements.get(id),
  createElement:()=>new Element(),querySelector:s=>s==='.tabs'?tabs:null};

// Import the real view objects and use app.js's registration order. Only
// their rendering lifecycle is replaced; ids/parents/nav/order are untouched.
const app=await readFile(new URL('js/app.js',base),'utf8');
const views={};
for(const [,names,module] of app.matchAll(/import (.+?) from '(\.\/views\/[^']+)';/g)){
  const imported=await import(new URL('js/'+module.slice(2),base));
  const defaultName=names.match(/^([A-Za-z]+View)/)?.[1];
  if(defaultName)views[defaultName]=imported.default;
  for(const name of names.match(/\{([^}]+)\}/)?.[1].split(',')||[]){
    if(imported[name])views[name]=imported[name];
  }
}
const registry=await import(new URL('js/core/registry.js',base));
const {initLensSwitch}=await import(new URL('js/core/lens-switch.js',base));
initLensSwitch();
for(const [,name,argument] of app.matchAll(/registerView\((\w+)(?:\((\w+)\))?\);/g)){
  const view=argument?views[name](views[argument]):views[name];
  assert.ok(view,'registered view '+name);
  registry.registerView({...view,mount:async()=>{},show:()=>{},hide:()=>{}});
}
registry.startRegistry({defaultView:'dashboard'});
const expectedTabs=['projects','agents','inbox','setup'];
assert.deepEqual(tabs.children.map(tab=>tab.id),expectedTabs.map(id=>'tab-'+id));
assert.deepEqual(buttons.map(button=>button.dataset.filesLens),['dashboard','projects','graph','tree','sharing','time']);
assert.deepEqual(buttons.map(button=>button.label),['Dashboard','List','Graph','Tree','Sharing','Timeline']);
assert.equal(tabs.children[0].innerHTML,'Projects');
assert.equal(elements.get('tab-agents').innerHTML,'Agents');
assert.equal(elements.has('tab-time'),false);
assert.equal(elements.has('tab-sessions'),false);
const initial=process.argv[1].replace(/^#\//,'');
const initialId=['dashboard','projects','graph','tree','sharing','time','agents','inbox','wiki','setup','secrets','connectors'].includes(initial)?initial:'dashboard';
function assertLens(id){
  assert.equal(location.hash,'#/'+id);
  assert.equal(pill.hidden,false);
  assert.ok(elements.get('tab-projects').classList.contains('is-on'));
  assert.deepEqual(buttons.filter(b=>b.attributes['aria-current']==='true').map(b=>b.dataset.filesLens),[id]);
  assert.ok(elements.get('view-'+(['dashboard','graph'].includes(id)?'graph':id)).classList.contains('is-active'));
}
function assertWiki(){
  assert.equal(location.hash,'#/wiki');
  assert.equal(pill.hidden,true);
  assert.equal(tabs.children.some(tab=>tab.classList.contains('is-on')),false);
  assert.ok(elements.get('view-wiki').classList.contains('is-active'));
}
function assertSetup(id){
  assert.equal(location.hash,'#/'+id);
  assert.equal(pill.hidden,true);
  assert.ok(elements.get('tab-setup').classList.contains('is-on'));
  assert.ok(elements.get('view-setup').classList.contains('is-active'));
  assert.equal(elements.has('tab-connectors'),false);
  assert.equal(elements.has('view-connectors'),false,'Connectors shares the persistent Setup section');
}
if(initialId==='wiki')assertWiki();
else if(['setup','secrets','connectors'].includes(initialId))assertSetup(initialId);
else if(['agents','inbox'].includes(initialId)){
  assert.equal(location.hash,'#/'+initialId);
  assert.equal(pill.hidden,true);
  assert.ok(elements.get('tab-'+initialId).classList.contains('is-on'));
  assert.ok(elements.get('view-'+initialId).classList.contains('is-active'));
}
else assertLens(initialId);
for(const button of buttons){
  pill.listeners.click({target:button});
  dispatchEvent(new CustomEvent('hashchange'));
  assertLens(button.dataset.filesLens);
}
for(const [index,id] of expectedTabs.entries()){
  dispatchEvent({type:'keydown',key:String(index+1)});
  assert.equal(location.hash,'#/'+id);
  assert.ok(elements.get('tab-'+id).classList.contains('is-on'));
  assert.equal(pill.hidden,id!=='projects');
}
for(const tagName of ['INPUT','TEXTAREA','SELECT']){
  document.activeElement={tagName};
  dispatchEvent({type:'keydown',key:'1'});
  assert.equal(location.hash,'#/setup');
}
document.activeElement=null;
await registry.switchTo('connectors');
assertSetup('connectors');
dispatchEvent({type:'keydown',key:'5'});
assertSetup('connectors'); // the legacy alias has no numbered shortcut
await registry.switchTo('wiki');
assertWiki();
dispatchEvent({type:'keydown',key:'7'});
assertWiki(); // Wiki is routable but does not consume a numbered shortcut
dispatchEvent({type:'keydown',key:'4'});
assert.equal(location.hash,'#/setup'); // Setup is the 4th tab now Timeline left the top bar
await registry.switchTo('dashboard');
elements.get('tab-projects').listeners.click();
assertLens('projects'); // keep the existing List route/tab action
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SpaceNavigationTests(unittest.TestCase):
    def test_default_deep_links_and_numbered_navigation(self) -> None:
        for route in ("", "#/dashboard", "#/projects", "#/graph", "#/tree", "#/sharing", "#/time", "#/agents", "#/inbox", "#/wiki", "#/setup", "#/secrets", "#/connectors", "#/sessions", "#/unknown"):
            with self.subTest(route=route):
                result = subprocess.run(
                    ["node", "--input-type=module", "-e", PROBE, "--", route],
                    cwd=ROOT, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
