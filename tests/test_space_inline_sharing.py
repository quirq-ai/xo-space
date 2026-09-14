"""Run the real inline sharing component against a small DOM and fake fetch.

No browser or live service is involved. Browser fixtures cover the two list
integrations; these probes cover write intent, deduplication and recovery.
"""
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRELUDE = r"""
import assert from 'node:assert/strict';
const events=[],requests=[];
globalThis.location={pathname:'/space/',search:'',hash:'#/projects/files/list'};
globalThis.CustomEvent=class{constructor(type,options={}){this.type=type;this.detail=options.detail;}};
globalThis.dispatchEvent=event=>events.push(event);
class Element{
  constructor(tag='div'){
    this.tagName=tag.toUpperCase();this.attributes={};this.dataset={};this.listeners=new Map();
    this.children=[];this.parentElement=null;this.hidden=false;this.disabled=false;
    this.value='';this.textContent='';this.selectionStart=0;this.selectionEnd=0;
  }
  set innerHTML(html){
    this.children=[];const stack=[this];
    for(const token of html.matchAll(/<\/?[^>]+>|[^<]+/g)){
      const part=token[0];
      if(part.startsWith('</')){stack.pop();continue;}
      if(!part.startsWith('<')){stack.at(-1).textContent+=part;continue;}
      const tag=part.match(/^<(\w+)/)[1],child=new Element(tag);
      for(const attr of part.slice(tag.length+1,-1).matchAll(/([\w-]+)(?:="([^"]*)")?/g)){
        child.setAttribute(attr[1],attr[2]??'');
        if(attr[1]==='hidden')child.hidden=true;
      }
      stack.at(-1).appendChild(child);
      if(!['input','br'].includes(tag))stack.push(child);
    }
  }
  setAttribute(name,value){this.attributes[name]=String(value);if(name==='id')this.id=String(value);}
  removeAttribute(name){delete this.attributes[name];}
  getAttribute(name){return this.attributes[name]??null;}
  appendChild(child){child.remove();this.children.push(child);child.parentElement=this;return child;}
  remove(){const siblings=this.parentElement?.children;if(siblings)siblings.splice(siblings.indexOf(this),1);this.parentElement=null;}
  querySelector(selector){
    const match=node=>selector.startsWith('[')?Object.hasOwn(node.attributes,selector.slice(1,-1)):node.tagName===selector.toUpperCase();
    for(const child of this.children){if(match(child))return child;const nested=child.querySelector(selector);if(nested)return nested;}
    return null;
  }
  addEventListener(type,handler){if(!this.listeners.has(type))this.listeners.set(type,[]);this.listeners.get(type).push(handler);}
  async fire(type){const event={type,target:this,preventDefault(){this.prevented=true;}};
    await Promise.all((this.listeners.get(type)||[]).map(handler=>handler(event)));return event;}
  focus(){if(!this.disabled)document.activeElement=this;}
  scrollIntoView(){}
  get isConnected(){return this===document.body||Boolean(this.parentElement?.isConnected);}
  getClientRects(){for(let node=this;node;node=node.parentElement)if(node.hidden)return[];return this.isConnected?[{}]:[];}
}
globalThis.document={body:new Element('body'),createElement:tag=>new Element(tag),activeElement:null};
document.activeElement=document.body;
const gate=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return{promise,resolve,reject};};
let respond=()=>Promise.reject(new Error('No response fixture configured'));
globalThis.fetch=(url,options)=>{requests.push({url,options});return respond(url,options);};
const response=(data,{ok=true,status=200}={})=>({ok,status,json:async()=>data});
const settle=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
const {createProjectShare,isProjectSharing}=await import('../space_ui/js/core/project-share.js');
const create=(projectId='fictional-project',options={})=>{
  const controller=createProjectShare({projectId,...options}),trigger=new Element('button');
  document.body.appendChild(trigger);document.body.appendChild(controller.element);controller.setTrigger(trigger);
  const input=controller.element.querySelector('input'),submit=controller.element.querySelector('[data-share-submit]');
  const cancel=controller.element.querySelector('[data-share-cancel]'),result=controller.element.querySelector('[data-share-result]');
  return{...controller,trigger,input,submit,cancel,result};
};
"""


@unittest.skipUnless(shutil.which("node"), "node is unavailable")
class InlineSharingTests(unittest.TestCase):
    def probe(self, source: str) -> None:
        result = subprocess.run(
            ["node", "--input-type=module", "-e", PRELUDE + source],
            cwd=ROOT / "tests", capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_open_cancel_and_validation_never_share_or_navigate(self):
        self.probe(r"""
const drafts=[],form=create('fictional-project',{onDraftChange:value=>drafts.push(value)});
assert.equal(form.element.hidden,true);assert.equal(form.trigger.getAttribute('aria-expanded'),'false');
form.open();assert.equal(form.element.hidden,false);assert.equal(document.activeElement,form.input);
assert.equal(form.trigger.getAttribute('aria-controls'),form.element.id);
for(const value of ['', 'contains whitespace','x'.repeat(101)]){
  form.input.value=value;await form.element.fire('submit');
  assert.equal(requests.length,0);assert.equal(form.result.dataset.state,'error');
  assert.equal(form.input.getAttribute('aria-invalid'),'true');
}
form.input.value='recipient-draft';await form.input.fire('input');
assert.equal(form.input.getAttribute('aria-invalid'),null);assert.equal(form.hasDraft(),true);
form.open();assert.equal(form.input.value,'recipient-draft','Opening an already open form preserves its draft');
await form.cancel.fire('click');assert.equal(form.element.hidden,true);assert.equal(form.input.value,'');
assert.equal(document.activeElement,form.trigger);assert.equal(form.hasDraft(),false);
assert.deepEqual([drafts[0],drafts.at(-1)],[true,false]);
assert.equal(location.hash,'#/projects/files/list');assert.equal(events.length,0);
assert.equal(requests.length,0);
""")

    def test_confirmed_share_is_explicit_trimmed_encoded_and_not_repeated(self):
        self.probe(r"""
respond=async()=>response({ok:true,repo:'fixture/repo'});
const form=create('folder with spaces');form.open();form.input.value='  recipient-space  ';
await form.element.fire('submit');
assert.equal(requests.length,1);assert.equal(requests[0].url,'/api/xo-projects/folder%20with%20spaces/share');
assert.equal(requests[0].options.method,'POST');
assert.deepEqual(JSON.parse(requests[0].options.body),{workspace_id:'recipient-space'});
assert.equal(requests[0].options.headers['Content-Type'],'application/json');
assert.equal(form.result.dataset.state,'success');assert.match(form.result.textContent,/recipient-space/);
assert.equal(form.element.hidden,false);assert.equal(form.input.value,'  recipient-space  ');
assert.equal(form.hasDraft(),false);assert.equal(form.submit.disabled,true);
assert.deepEqual(events.map(event=>[event.type,event.detail]),[['space:project-access-changed',{project_id:'folder with spaces',action:'access'}]]);
await form.element.fire('submit');assert.equal(requests.length,1,'Same successful recipient is not resubmitted');
form.input.value='another-space';await form.input.fire('input');assert.equal(form.submit.disabled,false);
await form.element.fire('submit');assert.equal(requests.length,2);
assert.equal(location.hash,'#/projects/files/list');
""")

    def test_pending_share_is_deduplicated_across_both_list_instances(self):
        self.probe(r"""
const deferred=gate(),busyA=[],busyB=[];respond=()=>deferred.promise;
const a=create('fictional-project',{onBusyChange:value=>busyA.push(value)});
const b=create('fictional-project',{onBusyChange:value=>busyB.push(value)});
a.open();b.open();a.input.value='space-a';b.input.value='space-b';
const first=a.element.fire('submit');await settle();
assert.equal(isProjectSharing('fictional-project'),true);
for(const form of [a,b]){
  assert.equal(form.input.disabled,true);assert.equal(form.submit.disabled,true);assert.equal(form.cancel.disabled,true);
  assert.equal(form.element.getAttribute('aria-busy'),'true');
}
await a.element.fire('submit');await b.element.fire('submit');await a.cancel.fire('click');
assert.equal(requests.length,1);assert.equal(a.element.hidden,false);
deferred.resolve(response({ok:true,repo:'fixture/repo'}));await first;
assert.equal(isProjectSharing('fictional-project'),false);
assert.deepEqual(busyA,[true,false]);assert.deepEqual(busyB,[true,false]);
assert.equal(b.input.value,'space-b');assert.equal(b.submit.disabled,false,'The other draft is still usable');
""")

    def test_errors_and_unconfirmed_success_keep_the_draft_and_allow_retry(self):
        self.probe(r"""
const form=create();form.open();form.input.value='recipient-space';
for(const fixture of [
  ()=>Promise.resolve(response({detail:{message:'No Git origin configured'}},{ok:false,status:400})),
  ()=>Promise.resolve(response({})),
  ()=>Promise.resolve(response({ok:false})),
  ()=>Promise.reject(new TypeError('fixture offline')),
]){
  respond=fixture;await form.element.fire('submit');
  assert.equal(form.result.dataset.state,'error');assert.ok(form.result.textContent);
  assert.equal(form.input.value,'recipient-space');assert.equal(form.element.hidden,false);
  assert.equal(form.input.disabled,false);assert.equal(form.submit.disabled,false);assert.equal(form.hasDraft(),true);
  assert.equal(isProjectSharing('fictional-project'),false);assert.equal(events.length,0);
}
respond=async()=>response({ok:true,repo:'fixture/repo'});await form.element.fire('submit');
assert.equal(form.result.dataset.state,'success');assert.equal(events.length,1);
""")

    def test_external_management_lock_and_disposed_form_do_not_leak_busy(self):
        self.probe(r"""
const deferred=gate();respond=()=>deferred.promise;
const form=create();form.open();form.input.value='recipient-space';form.setDisabled(true);
await form.element.fire('submit');assert.equal(requests.length,0);
form.setDisabled(false);const submission=form.element.fire('submit');await settle();
form.destroy();assert.equal(form.element.isConnected,false);
deferred.resolve(response({ok:true,repo:'fixture/repo'}));await submission;
assert.equal(isProjectSharing('fictional-project'),false);
assert.equal(events.length,1,'A confirmed result still notifies access consumers after its row disappears');
const next=create();next.open();assert.equal(next.input.disabled,false);
assert.equal(next.input.value,'');
""")

    def test_retained_form_keeps_input_and_does_not_steal_focus_on_completion(self):
        self.probe(r"""
const deferred=gate();respond=()=>deferred.promise;
const form=create();form.open();form.input.value='recipient-space';
form.input.selectionStart=3;form.input.selectionEnd=7;
form.setLabel('Renamed project <literal>');form.setDisabled(false);
assert.equal(form.element.getAttribute('aria-label'),'Share Renamed project <literal>');
assert.equal(document.activeElement,form.input);assert.equal(form.input.selectionStart,3);assert.equal(form.input.selectionEnd,7);
const submission=form.element.fire('submit');await settle();
const other=document.body.appendChild(new Element('input'));other.focus();
deferred.resolve(response({ok:true,repo:'fixture/repo'}));await submission;
assert.equal(document.activeElement,other,'A background completion cannot move focus away from a later edit');
""")


if __name__ == "__main__":
    unittest.main()
