"""Exercise real section definitions, view factories and the registry under Node.

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
  constructor(tagName='DIV'){
    this.tagName=tagName.toUpperCase();
    this.dataset={};this.attributes={};this.listeners={};this.classes=new Set();
    this.classList={toggle:(name,on)=>on?this.classes.add(name):this.classes.delete(name),
      contains:name=>this.classes.has(name)};
  }
  addEventListener(type,fn){this.listeners[type]=fn;}
  setAttribute(name,value){this.attributes[name]=value;}
  removeAttribute(name){delete this.attributes[name];}
  scrollIntoView(){}
  appendChild(child){elements.set(child.id,child);}
  replaceChildren(...children){this.children=children;children.forEach(child=>this.appendChild(child));}
}
const stage=new Element(),tabs=new Element();
elements.set('stage',stage);
globalThis.document={activeElement:null,getElementById:id=>elements.get(id),
  createElement:tag=>new Element(tag),querySelector:s=>s==='.tabs'?tabs:null,
  querySelectorAll:s=>s.startsWith('.tabs ')?tabs.children||[]:[]};

// Use the real view metadata and app registrations. Only rendering lifecycle
// is stubbed; the browser harness covers native secondary links and content.
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
const {PRIMARY_TABS,PROJECT_PAGES,PROJECT_SECTIONS,FILE_VIEWS,AGENT_PAGES,INBOX_PAGES}=await import(new URL('js/core/navigation.js',base));
const registered=[];
for(const [,name,argument,factory,factoryArgument] of app.matchAll(/registerView\((\w+)(?:\((\w+)\))?\);|(\w+)\((\w*)\)\.forEach\(registerView\);/g)){
  const result=factory?views[factory](factoryArgument?views[factoryArgument]:undefined):[argument?views[name](views[argument]):views[name]];
  assert.ok(Array.isArray(result),'view factory returns an array');
  for(const view of result){
    assert.ok(view,'registered view '+(name||factory));registered.push(view);
    registry.registerView({...view,mount:async()=>{},show:()=>{},hide:()=>{}});
  }
}
registry.startRegistry({tabs:PRIMARY_TABS,defaultView:'projects'});
assert.equal(history.length,1,'Initial deep links normalize in place');
assert.equal(historyPushes,0,'Starting the registry never pushes browser history');
const expectedTabs=['projects','agents','inbox','setup'];
const defaults=['projects/overview','agents/overview','inbox/items','setup/workspace'];
assert.deepEqual(PRIMARY_TABS.map(tab=>[tab.id,tab.defaultView]),expectedTabs.map((id,i)=>[id,defaults[i]]));
assert.deepEqual(tabs.children.map(tab=>[tab.id,tab.tagName,tab.href]),expectedTabs.map((id,i)=>['tab-'+id,'A','#/'+defaults[i]]));
assert.deepEqual(tabs.children.map(tab=>tab.textContent),['Projects','Agents','Inbox','Setup']);
assert.deepEqual(PROJECT_PAGES.map(page=>[page.id,page.route,page.label]),[
  ['dashboard','projects/overview','Overview'],['project-list','projects/files/list','List'],
  ['graph','projects/files/graph','Graph'],['tree','projects/files/tree','Tree'],
  ['time','projects/timeline','Timeline']]);
assert.deepEqual(PROJECT_SECTIONS.map(page=>[page.id,page.route,page.label]),[
  ['dashboard','projects/overview','Overview'],['files','projects/files','Files'],
  ['time','projects/timeline','Timeline']]);
assert.deepEqual(FILE_VIEWS.map(page=>page.id),['project-list','graph','tree']);
assert.deepEqual(AGENT_PAGES.map(page=>page.route),['overview','sessions','tools','models','trends'].map(page=>'agents/'+page));
assert.deepEqual(INBOX_PAGES.map(page=>page.route),['items','connections','jobs','activity','sharing-activity','sharing'].map(page=>'inbox/'+page));
assert.equal(registered.some(view=>view.id==='projects'),false,'List cannot own the Projects section identity');
assert.equal(registered.find(view=>view.id==='project-list').section,'projects');
assert.equal(elements.has('tab-project-list'),false,'List has no primary tab');
const setupRoutes=['workspace','intelligence','projects','connectors','secrets','commands','server'].map(id=>'setup/'+id);
const pages=[...PROJECT_PAGES,...AGENT_PAGES,...INBOX_PAGES,...setupRoutes.map(route=>({id:route,route}))];
const aliases={projects:'projects/overview',agents:'agents/overview',sessions:'agents/overview',inbox:'inbox/items',
  setup:'setup/workspace',dashboard:'projects/overview',list:'projects/files/list',graph:'projects/files/graph',tree:'projects/files/tree',
  'projects/files':'projects/files/list','projects/list':'projects/files/list',
  'projects/graph':'projects/files/graph','projects/tree':'projects/files/tree',
  sharing:'inbox/sharing','projects/sharing':'inbox/sharing',time:'projects/timeline',timeline:'projects/timeline',
  secrets:'setup/secrets',connectors:'setup/connectors',quirq:'setup/server/details'};
function canonical(target){return aliases[target]||pages.find(page=>page.id===target)?.route||target;}
function assertPage(route){
  assert.equal(location.hash,'#/'+route);
  const view=registered.find(view=>(view.route||view.id)===route);
  assert.ok(view,'registered canonical route '+route);
  const parent=view.parent||view.id;
  const active=tabs.children.filter(tab=>tab.classList.contains('is-on'));
  assert.deepEqual(active.map(tab=>tab.id),route==='wiki'?[]:['tab-'+parent]);
  assert.deepEqual(tabs.children.filter(tab=>tab.attributes['aria-current']==='page').map(tab=>tab.id),active.map(tab=>tab.id));
  const section=view.section||view.id;
  assert.ok(elements.get('view-'+section).classList.contains('is-active'));
  assert.equal([...elements].filter(([id,element])=>id.startsWith('view-')&&element.classList.contains('is-active')).length,1);
}
const initial=process.argv[1].replace(/^#\//,'');
const initialRoute=canonical(initial);
assertPage(registered.some(view=>(view.route||view.id)===initialRoute)?initialRoute:'projects/overview');
for(const page of pages){await registry.switchTo(page.route);assertPage(page.route);}
for(const [alias,route] of Object.entries(aliases)){await registry.switchTo(alias);assertPage(route);}
await registry.switchTo('wiki');assertPage('wiki');
for(const [i,id] of expectedTabs.entries()){
  dispatchEvent({type:'keydown',key:String(i+1)});assertPage(defaults[i]);
}
for(const tagName of ['INPUT','TEXTAREA','SELECT']){
  document.activeElement={tagName};dispatchEvent({type:'keydown',key:'1'});assertPage('setup/workspace');
}
document.activeElement=null;
dispatchEvent({type:'keydown',key:'5'});assertPage('setup/workspace');
await registry.switchTo('projects/files/list');
const primary=elements.get('tab-projects'),click=overrides=>({button:0,preventDefault(){this.prevented=true;},...overrides});
for(const modifiers of [{metaKey:true},{ctrlKey:true},{shiftKey:true},{button:1}]){
  const event=click(modifiers);primary.listeners.click(event);assert.equal(event.prevented,undefined);assertPage('projects/files/list');
}
const ordinary=click({});primary.listeners.click(ordinary);assert.equal(ordinary.prevented,true);assertPage('projects/overview');

// History is per page, including pages sharing one mounted DOM section.
await registry.switchTo('agents/overview');const start=historyPosition;
await registry.switchTo('agents/sessions');await registry.switchTo('inbox/jobs');
assert.equal(historyPosition,start+2);const pushes=historyPushes;
await registry.switchTo('inbox/jobs');assert.equal(historyPushes,pushes);
history.back();assertPage('agents/sessions');history.back();assertPage('agents/overview');
history.forward();assertPage('agents/sessions');assert.equal(historyPushes,pushes);
location.hash='#/secrets';const length=history.length,aliasPushes=historyPushes;
dispatchEvent(new CustomEvent('hashchange'));assertPage('setup/secrets');
assert.equal(history.length,length);assert.equal(historyPushes,aliasPushes);
await registry.switchTo('secrets');assert.equal(historyPushes,aliasPushes);
history.back();assertPage('agents/sessions');history.forward();assertPage('setup/secrets');
await registry.switchTo('projects/overview');
const fileStart=historyPosition;
await registry.switchTo('projects/files');assertPage('projects/files/list');
await registry.switchTo('projects/files/graph');await registry.switchTo('projects/files/tree');
assert.equal(historyPosition,fileStart+3,'Each Files mode gets one history entry');
history.back();assertPage('projects/files/graph');history.back();assertPage('projects/files/list');
history.forward();assertPage('projects/files/graph');
location.hash='#/projects/tree';const legacyLength=history.length,legacyPushes=historyPushes;
dispatchEvent(new CustomEvent('hashchange'));assertPage('projects/files/tree');
assert.equal(history.length,legacyLength);assert.equal(historyPushes,legacyPushes,'Legacy Files routes normalize in place');
assert.equal(elements.has('view-setup/projects'),false,'Setup routes share a persistent section');
assert.equal(elements.has('view-agents-sessions'),false,'Agents pages share a persistent section');
assert.equal(elements.has('view-inbox-jobs'),false,'Inbox rows, connections and jobs share a persistent section');
assert.equal(registered.find(view=>view.id==='sharing').parent,'inbox');
assert.equal(registered.find(view=>view.id==='inbox-activity').section,'inbox-activity');
assert.equal(registered.filter(view=>view.id==='sharing').length,1);
assert.equal(registered.filter(view=>view.id==='inbox-activity').length,1);

// Re-registering an independent view removes obsolete aliases.
registry.registerView({id:'route-probe',route:'probe/first',aliases:['probe-old'],nav:false,section:'setup',parent:'setup',mount:async()=>{}});
await registry.switchTo('probe-old');assert.equal(location.hash,'#/probe/first');
registry.registerView({id:'route-probe',route:'probe/second',aliases:['probe-new'],nav:false,section:'setup',parent:'setup',mount:async()=>{}});
await registry.switchTo('probe-old');assert.equal(location.hash,'#/probe/first');
await registry.switchTo('route-probe');assert.equal(location.hash,'#/probe/second');
await registry.switchTo('probe-new');assert.equal(location.hash,'#/probe/second');

"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SpaceNavigationTests(unittest.TestCase):
    def test_default_deep_links_and_numbered_navigation(self) -> None:
        for route in ("", "#/projects", "#/projects/overview", "#/projects/files", "#/projects/files/list", "#/projects/files/graph", "#/projects/files/tree", "#/projects/list", "#/projects/graph", "#/projects/tree", "#/dashboard", "#/list", "#/graph", "#/tree", "#/sharing", "#/time", "#/timeline", "#/agents", "#/agents/overview", "#/agents/sessions", "#/agents/tools", "#/agents/models", "#/agents/trends", "#/inbox", "#/inbox/items", "#/inbox/connections", "#/inbox/jobs", "#/inbox/activity", "#/inbox/sharing-activity", "#/inbox/sharing", "#/projects/sharing", "#/wiki", "#/setup", "#/setup/workspace", "#/setup/intelligence", "#/setup/projects", "#/setup/connectors", "#/setup/secrets", "#/setup/commands", "#/setup/server", "#/setup/server/details", "#/quirq", "#/secrets", "#/connectors", "#/sessions", "#/unknown"):
            with self.subTest(route=route):
                result = subprocess.run(
                    ["node", "--input-type=module", "-e", PROBE, "--", route],
                    cwd=ROOT, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
