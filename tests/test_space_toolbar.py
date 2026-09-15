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
/* Both History API writes change the URL without emitting hashchange. */
globalThis.history={
  pushState:(_state,_title,hash)=>{location.hash=hash;},
  replaceState:(_state,_title,hash)=>{location.hash=hash;},
};
globalThis.requestAnimationFrame=fn=>queueMicrotask(fn);
Object.defineProperty(globalThis,'navigator',{value:{platform:'Linux',userAgent:'node'},configurable:true});

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
const trigger=node('cmdk-trigger','BUTTON',controls);
node('cmdk-trigger-kbd','KBD',trigger);
const sectionNav=node('section-nav');
const graphRoot=node('graph-root','DIV',sectionNav);
const localSearch=node('view-search-wrap','DIV',controls);
const input=node('view-search','INPUT',localSearch),clear=node('view-search-clear','BUTTON',localSearch);
node('view-search-hint','KBD',localSearch);
node('root-btn','BUTTON',graphRoot);
const rootInput=node('root-q','INPUT',graphRoot);
node('rootdd','DIV',graphRoot);node('root-ac','DIV',graphRoot);
const stage=node('stage'),tabs=node('tabs');
globalThis.document={activeElement:null,
  getElementById:id=>elements.get(id)||null,
  querySelector:selector=>selector==='.topbar'?topbar:selector==='.tabs'?tabs:null,
  createElement:tag=>new Element('',tag.toUpperCase()),
  querySelectorAll:selector=>selector.startsWith('.tabs ')?tabs.children:[],
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
assert.equal(topbar.dataset.toolbar,'graph');assert.equal(controls.hidden,true);
assert.equal(key('/').defaultPrevented,true);assert.equal(document.activeElement,null);
await registry.switchTo('projects');
assert.equal(input.value,'retained');
gate.resolve();await loading;
assert.equal(graphShows,0);assert.equal(location.hash,'#/projects');
assert.equal(topbar.dataset.toolbar,'search');
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
assert.equal(topbar.dataset.toolbar,'graph');assert.equal(controls.hidden,true);
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

    def test_search_field_is_active_only_and_shortcuts_follow_current_view(self) -> None:
        self.run_probe(r"""
/* The Cmd+K trigger sits with Wiki and GitHub; the inline field is
   revealed only while a page filter is active. `/` opens the palette on a
   searchable page and on Graph. */
let firstQuery='',secondQuery='saved',secondContext,searchable=true;
let opens=0;addEventListener('space:open-command-palette',()=>opens++);
register({id:'first',toolbar:{search:{placeholder:'Find projects…',
  getValue:()=>firstQuery,setValue:value=>firstQuery=value}},mount(){}});
register({id:'second',toolbar:()=>searchable?{search:{placeholder:'Find sessions…',
  getValue:()=>secondQuery,setValue:value=>secondQuery=value}}:null,
  mount(_el,ctx){secondContext=ctx;}});
register({id:'graph',toolbar:{graph:true},mount(){}});
register({id:'wiki',mount(){}});
/* first has no query yet: only the trigger shows, the inline field is hidden,
   and `/` opens the palette rather than focusing a hidden field */
await registry.switchTo('first');
assert.equal(topbar.dataset.toolbar,'search');
assert.equal(localSearch.hidden,true);assert.equal(controls.hidden,true);
assert.equal(key('/').defaultPrevented,true);assert.equal(opens,1);
assert.equal(document.activeElement,null);
/* setting a value (as the palette's page search does) reveals the field */
input.value='aurora';input.fire('input');
assert.equal(firstQuery,'aurora');assert.equal(localSearch.hidden,false);assert.equal(controls.hidden,false);assert.equal(input.value,'aurora');
/* a page arriving with a saved query shows the field straight away */
await registry.switchTo('second');assert.equal(localSearch.hidden,false);assert.equal(input.value,'saved');
assert.equal(input.attributes['aria-label'],'Find sessions');
/* Escape in the field clears the query, which hides the field and drops focus */
input.focus();let escapedOutside=0;
addEventListener('keydown',event=>{if(event.key==='Escape')escapedOutside++;});
input.fire('keydown',{key:'Escape'});
assert.equal(secondQuery,'');assert.equal(localSearch.hidden,true);
assert.equal(document.activeElement,null);assert.equal(escapedOutside,0);
/* the clear button clears the active first-page query and hides the field */
await registry.switchTo('first');assert.equal(input.value,'aurora');assert.equal(localSearch.hidden,false);
clear.fire('click');assert.equal(firstQuery,'');assert.equal(localSearch.hidden,true);
/* modifiers, typing and contentEditable never let `/` act */
await registry.switchTo('second');secondQuery='saved';secondContext.refreshToolbar();
document.activeElement=null;const before=opens;
for(const flag of ['ctrlKey','metaKey','altKey','isComposing','defaultPrevented']){
  const event=key('/',{[flag]:true});
  if(flag!=='defaultPrevented')assert.equal(event.defaultPrevented,false);
}
assert.equal(opens,before,'modified / must not open the palette');
for(const tagName of ['INPUT','TEXTAREA','SELECT']){
  const other=new Element('',tagName);document.activeElement=other;
  assert.equal(key('/').defaultPrevented,false);assert.equal(document.activeElement,other);
}
const editable=new Element();editable.isContentEditable=true;document.activeElement=editable;
assert.equal(key('/').defaultPrevented,false);assert.equal(document.activeElement,editable);
/* a plain / on a searchable page opens the palette */
document.activeElement=null;key('/');assert.equal(opens,before+1);
/* a page with no search leaves / alone entirely */
searchable=false;secondContext.refreshToolbar();
assert.equal(controls.hidden,true);assert.equal(localSearch.hidden,true);
document.activeElement=null;assert.equal(key('/').defaultPrevented,false);assert.equal(opens,before+1);
/* Graph: / opens the palette; there is no navbar map field */
graphRoot.hidden=true; // The independent Projects root controller owns this state.
await registry.switchTo('graph');
assert.equal(localSearch.hidden,true);assert.equal(graphRoot.hidden,true);
assert.equal(controls.hidden,true);
const graphOpens=opens;key('/');assert.equal(opens,graphOpens+1);assert.equal(document.activeElement,null);
await registry.switchTo('wiki');
assert.equal(controls.hidden,true);assert.equal(document.activeElement,null);
assert.equal(key('/').defaultPrevented,false);
secondContext.refreshToolbar();assert.equal(topbar.dataset.toolbar,'none');
/* the navbar trigger opens the palette on click */
const clickOpens=opens;trigger.fire('click');assert.equal(opens,clickOpens+1);
""")
