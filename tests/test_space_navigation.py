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
const historyEntries=[process.argv[1]];
let historyPosition=0,historyPushes=0,historyReplaces=0;
globalThis.location={pathname:'/space/',search:'',
  get hash(){return historyEntries[historyPosition];},
  set hash(value){
    if(value===this.hash)return;
    historyEntries.splice(historyPosition+1);historyEntries.push(value);historyPosition++;
  },
};
globalThis.localStorage={getItem:()=>null};
globalThis.addEventListener=(type,fn)=>{
  const listeners=events.get(type)||[];listeners.push(fn);events.set(type,listeners);
};
globalThis.dispatchEvent=e=>{for(const fn of events.get(e.type)||[])fn(e);};
globalThis.CustomEvent=class{constructor(type,options={}){this.type=type;this.detail=options.detail;}};
globalThis.history={
  get length(){return historyEntries.length;},
  replaceState(_state,_title,hash){historyEntries[historyPosition]=hash;historyReplaces++;},
  pushState(_state,_title,hash){location.hash=hash;historyPushes++;},
  back(){if(historyPosition>0){historyPosition--;dispatchEvent(new CustomEvent('hashchange'));}},
  forward(){if(historyPosition<historyEntries.length-1){historyPosition++;dispatchEvent(new CustomEvent('hashchange'));}},
};
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
  for(const name of names.match(/\{([^}]+)\}/)?.[1].split(',').map(value=>value.trim())||[]){
    if(imported[name])views[name]=imported[name];
  }
}
const registry=await import(new URL('js/core/registry.js',base));
const {initLensSwitch}=await import(new URL('js/core/lens-switch.js',base));
initLensSwitch();
for(const [,name,argument,factory,factoryArgument] of app.matchAll(/registerView\((\w+)(?:\((\w+)\))?\);|(\w+)\((\w+)\)\.forEach\(registerView\);/g)){
  const registered=factory?views[factory](views[factoryArgument]):[argument?views[name](views[argument]):views[name]];
  assert.ok(Array.isArray(registered),'view factory returns an array');
  for(const view of registered){
    assert.ok(view,'registered view '+(name||factory));
    registry.registerView({...view,mount:async()=>{},show:()=>{},hide:()=>{}});
  }
}
registry.startRegistry({defaultView:'dashboard'});
assert.equal(history.length,1,'Initial deep links normalize in place');
assert.equal(historyPushes,0,'Starting the registry never pushes browser history');
const expectedTabs=['projects','agents','inbox','setup'];
assert.deepEqual(tabs.children.map(tab=>tab.id),expectedTabs.map(id=>'tab-'+id));
assert.deepEqual(buttons.map(button=>button.dataset.filesLens),['dashboard','projects','graph','tree','sharing','time']);
assert.deepEqual(buttons.map(button=>button.label),['Dashboard','List','Graph','Tree','Sharing','Timeline']);
assert.equal(tabs.children[0].innerHTML,'Projects');
assert.equal(elements.get('tab-agents').innerHTML,'Agents');
assert.equal(elements.has('tab-time'),false);
assert.equal(elements.has('tab-sessions'),false);
const initial=process.argv[1].replace(/^#\//,'');
const setupRoutes=['setup/workspace','setup/intelligence','setup/projects','setup/connectors','setup/secrets','setup/commands','setup/server'];
const aliases={setup:'setup/workspace',secrets:'setup/secrets',connectors:'setup/connectors'};
const initialId=aliases[initial]||([...setupRoutes,'dashboard','projects','graph','tree','sharing','time','agents','inbox','wiki'].includes(initial)?initial:'dashboard');
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
  assert.equal(elements.has('view-setup/projects'),false,'Setup children share one section instead of duplicating form containers');
}
if(initialId==='wiki')assertWiki();
else if(setupRoutes.includes(initialId))assertSetup(initialId);
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
  assert.equal(location.hash,'#/'+(id==='setup'?'setup/workspace':id));
  assert.ok(elements.get('tab-'+id).classList.contains('is-on'));
  assert.equal(pill.hidden,id!=='projects');
}
for(const tagName of ['INPUT','TEXTAREA','SELECT']){
  document.activeElement={tagName};
  dispatchEvent({type:'keydown',key:'1'});
  assert.equal(location.hash,'#/setup/workspace');
}
document.activeElement=null;
await registry.switchTo('connectors');
assertSetup('setup/connectors');
dispatchEvent({type:'keydown',key:'5'});
assertSetup('setup/connectors'); // the legacy alias has no numbered shortcut
await registry.switchTo('wiki');
assertWiki();
dispatchEvent({type:'keydown',key:'7'});
assertWiki(); // Wiki is routable but does not consume a numbered shortcut
dispatchEvent({type:'keydown',key:'4'});
assert.equal(location.hash,'#/setup/workspace'); // Setup is the 4th tab now Timeline left the top bar
await registry.switchTo('dashboard');
elements.get('tab-projects').listeners.click();
assertLens('projects'); // keep the existing List route/tab action

// Every Setup panel is independently addressable, without changing its parent
// tab or recreating the shared section. ID and route both reach the base view.
for(const route of setupRoutes){await registry.switchTo(route);assertSetup(route);}
await registry.switchTo('setup');assertSetup('setup/workspace');
const workspacePosition=historyPosition;
await registry.switchTo('setup/intelligence');
await registry.switchTo('setup/projects');
assert.equal(historyPosition,workspacePosition+2,'Explicit panel navigation creates Back destinations');
const pushes=historyPushes;
await registry.switchTo('setup/projects');
assert.equal(historyPushes,pushes,'Reactivating the current canonical route does not duplicate history');
history.back();assertSetup('setup/intelligence');
history.back();assertSetup('setup/workspace');
history.forward();assertSetup('setup/intelligence');
assert.equal(historyPushes,pushes,'Back and Forward never push a replacement history entry');

// A typed legacy URL is one browser destination. Normalization replaces that
// destination, including when the underlying view is already selected.
location.hash='#/secrets';
const aliasLength=history.length,aliasPushes=historyPushes;
dispatchEvent(new CustomEvent('hashchange'));
assertSetup('setup/secrets');
assert.equal(history.length,aliasLength);
assert.equal(historyPushes,aliasPushes);
await registry.switchTo('secrets');
assert.equal(historyPushes,aliasPushes,'Switching through an alias to the current view creates no duplicate');
history.back();assertSetup('setup/intelligence');
history.forward();assertSetup('setup/secrets');

// Generic contracts remain independent of Setup and clear removed aliases
// when a stable view ID is re-registered with a different URL.
registry.registerView({id:'route-probe',route:'probe/first',aliases:['probe-old'],nav:false,section:'setup',parent:'setup',mount:async()=>{}});
await registry.switchTo('probe-old');
assert.equal(location.hash,'#/probe/first');
registry.registerView({id:'route-probe',route:'probe/second',aliases:['probe-new'],nav:false,section:'setup',parent:'setup',mount:async()=>{}});
await registry.switchTo('probe-old');
assert.equal(location.hash,'#/probe/first','Removed aliases no longer resolve');
await registry.switchTo('route-probe');
assert.equal(location.hash,'#/probe/second','Stable IDs continue to resolve after a route change');
await registry.switchTo('probe-new');
assert.equal(location.hash,'#/probe/second');
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SpaceNavigationTests(unittest.TestCase):
    def test_default_deep_links_and_numbered_navigation(self) -> None:
        for route in ("", "#/dashboard", "#/projects", "#/graph", "#/tree", "#/sharing", "#/time", "#/agents", "#/inbox", "#/wiki", "#/setup", "#/setup/workspace", "#/setup/intelligence", "#/setup/projects", "#/setup/connectors", "#/setup/secrets", "#/setup/commands", "#/setup/server", "#/secrets", "#/connectors", "#/sessions", "#/unknown"):
            with self.subTest(route=route):
                result = subprocess.run(
                    ["node", "--input-type=module", "-e", PROBE, "--", route],
                    cwd=ROOT, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
