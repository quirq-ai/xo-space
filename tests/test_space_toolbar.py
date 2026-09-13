"""Exercise toolbar ownership and lazy view activation under Node.

The real controller and registry run against a small DOM. Deferred mounts
cover navigation races without timers, data fixtures or a canvas emulator.
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

const events=new Map(),elements=new Map();
globalThis.addEventListener=(type,fn)=>{
  const list=events.get(type)||[];list.push(fn);events.set(type,list);
};
globalThis.dispatchEvent=event=>{
  for(const fn of events.get(event.type)||[])fn(event);
};
globalThis.CustomEvent=class{constructor(type,options={}){this.type=type;this.detail=options.detail;}};
globalThis.location={hash:''};
globalThis.history={replaceState:(_state,_title,hash)=>{location.hash=hash;}};
globalThis.requestAnimationFrame=fn=>queueMicrotask(fn);

class Element{
  constructor(id='',tagName='DIV'){
    this.id=id;this.tagName=tagName;this.dataset={};this.attributes={};
    this.listeners=new Map();this.children=[];this.value='';this.hidden=false;
    this.disabled=false;this.style={};this.classes=new Set();
    this.classList={
      add:name=>this.classes.add(name),remove:name=>this.classes.delete(name),
      contains:name=>this.classes.has(name),
      toggle:(name,on)=>on?this.classes.add(name):this.classes.delete(name),
    };
    if(id)elements.set(id,this);
  }
  addEventListener(type,fn){
    const list=this.listeners.get(type)||[];list.push(fn);this.listeners.set(type,list);
  }
  fire(type,details={}){
    const event={type,target:this,defaultPrevented:false,stopped:false,
      preventDefault(){this.defaultPrevented=true;},
      stopPropagation(){this.stopped=true;},...details};
    for(const fn of this.listeners.get(type)||[])fn(event);
    if(!event.stopped)dispatchEvent(event);
    return event;
  }
  setAttribute(name,value){this.attributes[name]=value;}
  removeAttribute(name){delete this.attributes[name];}
  appendChild(child){child.parent=this;this.children.push(child);if(child.id)elements.set(child.id,child);}
  replaceChildren(...children){this.children=[];children.forEach(child=>this.appendChild(child));}
  contains(element){for(let node=element;node;node=node.parent)if(node===this)return true;return false;}
  focus(){if(!this.disabled)document.activeElement=this;}
  blur(){if(document.activeElement===this)document.activeElement=null;}
  scrollIntoView(){}
}
const node=(id,tag='DIV',parent=null)=>{
  const el=new Element(id,tag);parent?.appendChild(el);return el;
};
const topbar=node('topbar'),controls=node('toolbar-controls','DIV',topbar);
const graphRoot=node('graph-root','DIV',controls),graphSearch=node('graph-search','DIV',controls);
const localSearch=node('view-search-wrap','DIV',controls);
const input=node('view-search','INPUT',localSearch),clear=node('view-search-clear','BUTTON',localSearch);
node('view-search-hint','KBD',localSearch);
const graphInput=node('q','INPUT',graphSearch);
node('root-btn','BUTTON',graphRoot);
node('rootdd','DIV',graphRoot);node('root-ac','DIV',graphRoot);node('qac','DIV',graphSearch);
const meta=node('fmeta'),stage=node('stage'),tabs=node('tabs');
globalThis.document={activeElement:null,
  getElementById:id=>elements.get(id)||null,
  querySelector:selector=>selector==='.topbar'?topbar:selector==='.tabs'?tabs:null,
  createElement:tag=>new Element('',tag.toUpperCase()),
};
const base=pathToFileURL(process.cwd()+'/space_ui/js/core/');
const registry=await import(new URL('registry.js',base));
const {initToolbar}=await import(new URL('toolbar.js',base));
initToolbar();
const register=view=>{
  const section=view.section||view.id;
  if(!elements.has('view-'+section))node('view-'+section,'SECTION',stage);
  node('tab-'+(view.parent||view.id),'BUTTON',tabs);
  registry.registerView(view);return view;
};
const deferred=()=>{
  let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});
  return{promise,resolve,reject};
};
const flush=async()=>{await Promise.resolve();await Promise.resolve();};
const key=(value,details={})=>{
  const event={type:'keydown',key:value,defaultPrevented:false,
    preventDefault(){this.defaultPrevented=true;},...details};
  dispatchEvent(event);return event;
};
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SpaceToolbarTests(unittest.TestCase):
    def run_probe(self, source: str) -> None:
        result = subprocess.run(
            ["node", "--input-type=module", "-e", PRELUDE + source],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_leaving_slow_mount_keeps_new_view_and_toolbar(self) -> None:
        self.run_probe(r"""
const gate=deferred();let ready=false,graphShows=0,graphContext;
register({id:'graph',toolbar:()=>({graph:true,disabled:!ready}),
  async mount(_el,ctx){graphContext=ctx;await gate.promise;ready=true;},
  show(){graphShows++;},hide(){}});
let query='retained';
register({id:'projects',toolbar:{search:{placeholder:'Find projects…',
  getValue:()=>query,setValue:value=>query=value}},mount(){}});
const loading=registry.switchTo('graph');
assert.equal(topbar.dataset.toolbar,'graph');assert.equal(graphInput.disabled,true);
assert.equal(key('/').defaultPrevented,false);assert.equal(document.activeElement,null);
await registry.switchTo('projects');
assert.equal(input.value,'retained');
gate.resolve();await loading;
assert.equal(graphShows,0);assert.equal(location.hash,'#/projects');
assert.equal(topbar.dataset.toolbar,'search');assert.equal(meta.hidden,true);
graphContext.refreshToolbar();
assert.equal(topbar.dataset.toolbar,'search');assert.equal(input.value,'retained');
""")

    def test_reentering_pending_mount_waits_and_shows_once(self) -> None:
        self.run_probe(r"""
const gate=deferred();let ready=false,mounts=0;const shows=[];
register({id:'graph',toolbar:()=>({graph:true,disabled:!ready}),
  async mount(){mounts++;await gate.promise;ready=true;},
  show(){shows.push(ready);},hide(){}});
register({id:'wiki',mount(){}});
const first=registry.switchTo('graph');
await registry.switchTo('wiki');
const reentry=registry.switchTo('graph');let settled=false;reentry.then(()=>settled=true);
await flush();
assert.equal(mounts,1);assert.deepEqual(shows,[],'show must wait for the first mount');
assert.equal(settled,false,'reentry must await the shared pending mount');
gate.resolve();await Promise.all([first,reentry]);
assert.deepEqual(shows,[true],'only the newest activation shows the ready view');
assert.equal(topbar.dataset.toolbar,'graph');assert.equal(graphInput.disabled,false);
assert.equal(location.hash,'#/graph');
""")

    def test_shared_section_sibling_mount_cannot_reclaim_activation(self) -> None:
        self.run_probe(r"""
const gate=deferred(),shown=[];let oldContext;
register({id:'projects',mount(){}});
register({id:'dashboard',section:'graph',parent:'projects',toolbar:{graph:true},
  async mount(_el,ctx){oldContext=ctx;await gate.promise;},
  show(){shown.push('dashboard');},hide(){}});
register({id:'graph',section:'graph',parent:'projects',toolbar:{graph:true},
  mount(){},show(){shown.push('graph');},hide(){}});
const dashboard=registry.switchTo('dashboard');
await registry.switchTo('graph');
gate.resolve();await dashboard;oldContext.refreshToolbar();
assert.deepEqual(shown,['graph']);assert.equal(location.hash,'#/graph');
assert.equal(elements.get('view-graph').classList.contains('is-active'),true);
assert.equal(elements.get('tab-projects').classList.contains('is-on'),true);
assert.equal(topbar.dataset.toolbar,'graph');
""")

    def test_queries_clear_and_shortcuts_follow_current_view(self) -> None:
        self.run_probe(r"""
let firstQuery='',secondQuery='saved',secondContext,searchable=true;
register({id:'first',toolbar:{search:{placeholder:'Find projects…',
  getValue:()=>firstQuery,setValue:value=>firstQuery=value}},mount(){}});
register({id:'second',toolbar:()=>searchable?{search:{placeholder:'Find sessions…',
  getValue:()=>secondQuery,setValue:value=>secondQuery=value}}:null,
  mount(_el,ctx){secondContext=ctx;}});
register({id:'graph',toolbar:{graph:true},mount(){}});
register({id:'wiki',mount(){}});
await registry.switchTo('first');
assert.equal(key('/').defaultPrevented,true);assert.equal(document.activeElement,input);
input.value='aurora';input.fire('input');assert.equal(firstQuery,'aurora');
await registry.switchTo('second');assert.equal(input.value,'saved');
assert.equal(input.attributes['aria-label'],'Find sessions');
input.focus();let escapedOutside=0;
addEventListener('keydown',event=>{if(event.key==='Escape')escapedOutside++;});
input.fire('keydown',{key:'Escape'});
assert.equal(secondQuery,'');assert.equal(document.activeElement,input);assert.equal(escapedOutside,0);
input.fire('keydown',{key:'Escape'});assert.equal(document.activeElement,null);
await registry.switchTo('first');assert.equal(input.value,'aurora');
clear.fire('click');assert.equal(firstQuery,'');assert.equal(document.activeElement,input);
assert.equal(clear.hidden,true);
await registry.switchTo('second');
document.activeElement=null;
for(const flag of ['ctrlKey','metaKey','altKey','isComposing','defaultPrevented']){
  const event=key('/',{[flag]:true});
  assert.equal(document.activeElement,null,flag+' must not steal focus');
  if(flag!=='defaultPrevented')assert.equal(event.defaultPrevented,false);
}
for(const tagName of ['INPUT','TEXTAREA','SELECT']){
  const other=new Element('',tagName);document.activeElement=other;
  assert.equal(key('/').defaultPrevented,false);assert.equal(document.activeElement,other);
}
const editable=new Element();editable.isContentEditable=true;document.activeElement=editable;
assert.equal(key('/').defaultPrevented,false);assert.equal(document.activeElement,editable);
document.activeElement=null;key('/');assert.equal(document.activeElement,input);
searchable=false;secondContext.refreshToolbar();
assert.equal(controls.hidden,true);assert.equal(document.activeElement,null);
await registry.switchTo('graph');
assert.equal(localSearch.hidden,true);assert.equal(graphRoot.hidden,false);assert.equal(meta.hidden,false);
key('/');assert.equal(document.activeElement,graphInput);
for(const id of ['rootdd','qac','root-ac'])elements.get(id).classList.add('is-open');
await registry.switchTo('wiki');
assert.equal(controls.hidden,true);assert.equal(meta.hidden,true);assert.equal(document.activeElement,null);
assert.equal(key('/').defaultPrevented,false);
for(const id of ['rootdd','qac','root-ac'])assert.equal(elements.get(id).classList.contains('is-open'),false);
secondContext.refreshToolbar();assert.equal(topbar.dataset.toolbar,'none');
""")
