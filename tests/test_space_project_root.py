"""The Projects root picker can read and navigate without booting an atlas."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRELUDE = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {dataViewControls} from './space_ui/js/core/data-views.js';
const events=new Map(),elements=new Map();
class Element{
 constructor(id){this.id=id;this.value='';this.hidden=false;this.disabled=false;this.textContent='';this.innerHTML='';this.listeners=new Map();this.attributes={};this.children=[];
  const classes=new Set();this.classList={add:(...names)=>names.forEach(n=>classes.add(n)),remove:(...names)=>names.forEach(n=>classes.delete(n)),
   contains:name=>classes.has(name),toggle:(name,on)=>on?classes.add(name):classes.delete(name)};}
 addEventListener(type,handler){if(!this.listeners.has(type))this.listeners.set(type,[]);this.listeners.get(type).push(handler);}
 emit(type,event={}){for(const fn of this.listeners.get(type)||[])fn({target:this,preventDefault(){},stopPropagation(){},...event});}
 setAttribute(key,value){this.attributes[key]=value;}
 removeAttribute(key){delete this.attributes[key];}
 focus(){document.activeElement=this;}
 blur(){if(document.activeElement===this)document.activeElement=null;}
 contains(target){return target===this;}
 querySelector(selector){return selector.startsWith('#')?this.children.find(child=>child.id===selector.slice(1))||null:null;}
 querySelectorAll(){return[];}
 prepend(child){this.children.unshift(child);if(child.id)elements.set(child.id,child);}
}
const document={activeElement:null,documentElement:new Element('html'),
 getElementById:id=>{if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);},
 querySelectorAll:()=>[],createElement:()=>new Element('')};
document.documentElement.dataset={};
/* atlas.js recolours picker categories from CSS custom properties. The probe
   runs headless, so serve an empty palette: every token falls back to the
   colour the dataset already carries. */
const getComputedStyle=()=>({getPropertyValue:()=>''});
const context={document,console,Set,Map,setTimeout,clearTimeout,AbortController,dataViewControls,getComputedStyle,
 addEventListener:(name,handler)=>{if(!events.has(name))events.set(name,[]);events.get(name).push(handler);},
 esc:value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
};
vm.createContext(context);
const source=fs.readFileSync('space_ui/js/core/project-root.js','utf8').replace(/^import .*?;\n/gm,'').replaceAll('export function','function');
vm.runInContext(source,context);
const evaluate=source=>vm.runInContext(source,context);
const node=id=>document.getElementById(id);
const settle=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
const emit=(type,detail)=>{for(const fn of events.get(type)||[])fn({detail});};
const gate=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
const fixture=(root,label)=>({root:{id:root,label},meta:{hubLabel:'Project',collectionLabel:'Folder'},
 hubs:[{id:root+'-project',label:'Fixture project',cat:root+'-project'}],groups:[],leaves:[],ties:[]});
'''


@unittest.skipUnless(shutil.which('node'), 'node is unavailable')
class ProjectRootTests(unittest.TestCase):
    def probe(self, source):
        result = subprocess.run(['node', '--input-type=module', '-e', PRELUDE + source],
                                cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_lazy_reads_navigation_guard_keyboard_and_dataset_identity(self):
        self.probe(r'''
const reads=[],picked=[];
const create=evaluate('createProjectRootPicker');
const picker=create({readDataset:key=>{const pending=gate();reads.push({key,...pending});return pending.promise;},onPick:value=>picked.push(value)});
picker.setContext('graph');
assert.equal(node('graph-root').hidden,false);assert.equal(reads.length,0,'Visible picker alone never loads or navigates');
node('root-btn').emit('click');assert.equal(reads.length,1);assert.equal(node('root-q').disabled,true);
picker.setContext(null);reads[0].resolve(fixture('space-root','Files workspace'));await settle();
assert.equal(node('graph-root').hidden,true);assert.equal(node('rootdd').classList.contains('is-open'),false);
assert.equal(picked.length,0,'A late metadata read cannot navigate after leaving Projects');
picker.setContext('graph');node('root-btn').emit('click');reads[1].resolve(fixture('space-root','Files workspace'));await settle();
assert.equal(node('root-reset').textContent,'Reset to Files workspace');
node('root-q').value='Fixture';node('root-q').emit('input');node('root-q').emit('keydown',{key:'Enter'});
assert.deepEqual(JSON.parse(JSON.stringify(picked)),[{dataset:'graph',id:'space-root-project'}]);
assert.equal(node('rootdd').classList.contains('is-open'),false);
picker.setRoot('graph',{id:'space-root-project',label:'Chosen files project'});
picker.setData('dashboard',fixture('overview-root','Overview workspace'));picker.setContext('dashboard');
assert.equal(node('root-name').textContent,'Overview workspace');assert.equal(node('root-reset').textContent,'Reset to Overview workspace');
picker.setRoot('dashboard',{id:'overview-root-project',label:'Chosen overview project'});picker.setContext('graph');
assert.equal(node('root-name').textContent,'Chosen files project');assert.equal(node('root-reset').textContent,'Reset to Files workspace');
node('root-btn').emit('click');await settle();
node('root-q').value='Fixture';node('root-q').emit('input');
node('root-ac').emit('click',{detail:0,target:{closest:()=>({dataset:{rootIndex:'0'}})}});
assert.equal(picked.length,2,'Tab and Enter on a result button selects it too');
node('root-btn').emit('click');await settle();node('root-reset').emit('click');
assert.equal(picked.at(-1).id,'space-root','Reset uses the dataset root ID rather than a hardcoded XO');
assert.equal(node('root-btn').listeners.get('click').length,1,'Context changes never duplicate DOM listeners');
''')

    def test_direct_project_pages_boot_only_after_a_root_is_selected(self):
        self.probe(r'''
const graph=fixture('files-root','Files'),dashboard=fixture('overview-root','Overview');
const reads=[],boots=[],navigations=[],applied=[],focused=[];
Object.assign(context,{API_BASE:'',projectPage:id=>({id,route:id==='dashboard'?'projects/overview':'projects/data/graph'}),toast(){},
 localStorage:{getItem:()=>null,setItem(){}},apiFetch:async url=>{reads.push(url);return{ok:true,data:url.includes('dashboard')?dashboard:graph};},
 recordBoot:(dataset,data)=>boots.push([dataset,data.root.id]),recordRoot:id=>applied.push(id),recordFocus:id=>focused.push(id)});
let atlas=fs.readFileSync('space_ui/js/views/atlas.js','utf8').replace(/^import .*?;\n/gm,'').replaceAll('export const','const').replaceAll('export function','function');
atlas=atlas.slice(0,atlas.indexOf('function boot(DATA,DATA_SOURCE,bootDataset){'))+`
function boot(data,source,dataset){recordBoot(dataset,data);hooks.setRoot=id=>{recordRoot(id);return true;};hooks.focusProject=()=>{if(pendingFocus){recordFocus(pendingFocus);pendingFocus=null;}};hooks.setActiveView=view=>{if(view==='graph'){applyRootSelection(dataset);hooks.focusProject();}};}
globalThis.pages={graph:graphView,dashboard:dashboardView};`;
vm.runInContext(atlas,context);
const init=evaluate('initProjectRootPicker');
async function go(route){navigations.push(route);const id=route==='projects/overview'?'dashboard':'graph';
 emit('space:view',{id,tab:'projects'});const view=context.pages[id];await view.mount(node('view-graph'),{switchTo:go});await view.show();}
init({switchTo:go});
for(const id of ['project-list','tree','project-manage']){
 emit('space:view',{id,tab:'projects'});assert.equal(node('graph-root').hidden,false);
 node('root-btn').emit('click');await settle();assert.equal(boots.length,0,'Metadata reads never boot or replace an atlas on '+id);
 node('root-btn').emit('click');
}
assert.deepEqual(reads,['/xo/space.json'],'Pages share the lazy file metadata read');
emit('space:view',{id:'sharing',tab:'inbox'});assert.equal(node('graph-root').hidden,true,'Inbox Sharing has no Projects root picker');
emit('space:view',{id:'tree',tab:'projects'});
emit('space:focus-project','older-preview-target');
node('root-btn').emit('click');await settle();node('root-q').value='Fixture';node('root-q').emit('input');node('root-q').emit('keydown',{key:'Enter'});await settle();
assert.deepEqual(navigations,['projects/data/graph']);assert.deepEqual(boots,[['graph','files-root']]);assert.equal(applied.at(-1),'files-root-project');
assert.equal(node('view-graph').classList.contains('has-file-tools'),true);
assert.match(node('graph-file-toolbar').innerHTML,/data-data-mode="graph" aria-current="page"/);
assert.deepEqual(focused,[],'A newer root choice supersedes an older parked preview focus');
await go('projects/overview');assert.deepEqual(boots.at(-1),['dashboard','overview-root']);
assert.equal(node('graph-file-toolbar').hidden,true,'Overview hides the Files controls while preserving their DOM');
node('root-btn').emit('click');await settle();node('root-q').value='Fixture';node('root-q').emit('input');node('root-q').emit('keydown',{key:'Enter'});await settle();
assert.equal(applied.at(-1),'overview-root-project');assert.equal(navigations.length,2,'Overview selection retains its existing engine and route');
emit('space:view',{id:'tree',tab:'projects'});node('root-btn').emit('click');await settle();
node('root-q').value='Fixture';node('root-q').emit('input');node('root-q').emit('keydown',{key:'Enter'});
emit('space:focus-project','newer-preview-target');await settle();
assert.deepEqual(focused,['newer-preview-target'],'A newer preview focus supersedes a root choice whose engine is still loading');
assert.equal(applied.at(-1),'files-root','The superseded pending root must not be applied ahead of the newer focus');
assert.equal(node('view-graph').children.filter(child=>child.id==='graph-file-toolbar').length,1,'Returning to Graph reuses one toolbar container');
''')

    def test_timed_out_dataset_read_can_retry_without_navigating(self):
        self.probe(r'''
let reads=0,signal;
Object.assign(context,{API_BASE:'',projectPage:id=>({id}),toast(){},
 localStorage:{getItem:()=>null,setItem(){}},setTimeout:fn=>setTimeout(fn,5),
 apiFetch:async(_url,options)=>{reads++;signal=options.signal;if(reads===1)return new Promise(()=>{});return{ok:true,data:fixture('root','Fixture workspace')};}});
let atlas=fs.readFileSync('space_ui/js/views/atlas.js','utf8').replace(/^import .*?;\n/gm,'').replaceAll('export const','const').replaceAll('export function','function');
atlas=atlas.slice(0,atlas.indexOf('function boot(DATA,DATA_SOURCE,bootDataset){'))+'function boot(){throw new Error("Metadata read must never boot an atlas");}';
vm.runInContext(atlas,context);
evaluate('initProjectRootPicker')({switchTo:()=>assert.fail('Timeout/retry cannot navigate')});emit('space:view',{id:'tree',tab:'projects'});
node('root-btn').emit('click');await new Promise(resolve=>setTimeout(resolve,15));
assert.equal(signal.aborted,true,'The bounded request actively cancels its fetch');
assert.match(node('root-ac').innerHTML,/Could not load roots/);assert.equal(node('root-btn').disabled,false);
node('root-btn').emit('click');node('root-btn').emit('click');await settle();
assert.equal(reads,2,'Retry starts a new request rather than reusing a poisoned cache');
assert.equal(node('root-q').disabled,false);assert.equal(node('root-reset').textContent,'Reset to Fixture workspace');
''')

    def test_api_owned_cancellation_does_not_cancel_shared_reads(self):
        self.probe(r'''
const calls=[];let shared=0;
const api={location:{pathname:'/space/',search:''},TypeError,singleFlight:(_key,fetch)=>{shared++;return fetch();},
 fetch:async(path,options)=>{calls.push(options);return{ok:true,status:200,json:async()=>({})};}};
vm.createContext(api);vm.runInContext(fs.readFileSync('space_ui/js/core/api.js','utf8').replace(/^import .*?;\n/gm,'').replaceAll('export const','const').replaceAll('export function','function'),api);
const fetch=vm.runInContext('apiFetch',api),controller=new AbortController();
await fetch('/xo/space.json');await fetch('/xo/space.json',{signal:controller.signal});
assert.equal(shared,1,'Ordinary GETs keep single-flight sharing; independently cancellable reads bypass it');
assert.equal(calls[1].signal,controller.signal);assert.equal(calls[1].method,'GET');
''')


if __name__ == '__main__':
    unittest.main()
