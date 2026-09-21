"""Shared Agents/Inbox controllers retain state across registered subpages."""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRELUDE = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {pathToFileURL} from 'node:url';
const navigation=await import(pathToFileURL(process.cwd()+'/space_ui/js/core/navigation.js'));
function module(name,extra={},cores=[]){
  const context={console,Date,Set,Map,AGENT_PAGES:navigation.AGENT_PAGES,INBOX_PAGES:navigation.INBOX_PAGES,...extra};
  vm.createContext(context);
  /* a core module the view imports runs in the same context, so the probe
     exercises the one shared implementation rather than a stub */
  for(const core of cores){
    const src=fs.readFileSync('space_ui/js/core/'+core+'.js','utf8').replace(/^import .*?;\n/gm,'')
      .replace(/^export (const|function) /gm,'$1 ');
    vm.runInContext(src,context);
  }
  const source=fs.readFileSync('space_ui/js/views/'+name+'.js','utf8')
    .replace(/^import .*?;\n/gm,'').replaceAll('export async function','async function')
    .replaceAll('export function','function').replace(/^export const /gm,'const ').replace(/export default (\w+);/,'globalThis.legacy=$1;');
  vm.runInContext(source,context);
  return{context,evaluate:source=>vm.runInContext(source,context)};
}
const settle=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
class Element{
  constructor(){this.innerHTML='';this.hidden=false;this.attributes={};this.paints=0;}
  set innerHTML(value){this.html=value;this.paints=(this.paints||0)+1;}
  get innerHTML(){return this.html;}
  set outerHTML(value){this.innerHTML=value;}
  setAttribute(key,value){this.attributes[key]=value;}
  addEventListener(){}
  querySelector(){return null;}
  querySelectorAll(){return [];}
  contains(){return false;}
}
function inbox(){
  const nodes=new Map();
  for(const selector of ['.inb-items-page','.inb-jobs-page','.inb-page-head h1','.inb-page-actions','.inb-jobs'])nodes.set(selector,new Element());
  const root=new Element();root.querySelector=selector=>nodes.get(selector)||null;
  const requests=[],intervals=new Map(),opened=[],events=[],results=[];
  const mod=module('inbox',{
    API_BASE:'',document:{activeElement:null,getElementById:()=>null},
    CSS:{escape:value=>value},CustomEvent:class{constructor(type,{detail}){this.type=type;this.detail=detail;}},
    dispatchEvent:event=>events.push(event),
    apiFetch:(path,opts)=>new Promise(resolve=>requests.push({path,opts,resolve})),
    clearSlottedInterval:name=>intervals.delete(name),
    setSlottedInterval:(name,fn,ms)=>intervals.set(name,{fn,ms}),
    esc:value=>String(value??''),pills:()=>'',rel:()=>'',toast(){},
    collectorLabels:()=>'',every:()=>'',pollLine:()=>({text:'Up to date'}),accountLabel:()=>'',
    openCommandResults:job=>results.push(job),
    describeSchedule:job=>job.every_seconds==null?'Manual':'Every minute',
    describeOnce:()=>'Runs when you click Run now',
    isScheduled:job=>job?.every_seconds!=null,statusText:status=>String(status),
  },[]);
  const views=Array.from(mod.evaluate('createInboxViews()'));
  const page=name=>views.find(view=>view.route==='inbox/'+name);
  const mount=()=>Promise.all(views.map(view=>view.mount(root,{switchTo:route=>opened.push(route)})));
  return{...mod,nodes,root,requests,intervals,opened,events,results,page,mount};
}
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SpaceSubpageControllerTests(unittest.TestCase):
    def run_probe(self, source: str) -> None:
        result = subprocess.run(
            ["node", "--input-type=module", "-e", PRELUDE + source],
            cwd=ROOT, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_agents_share_mount_and_refresh_the_active_leaf_toolbar(self) -> None:
        self.run_probe(r"""
let release;const gate=new Promise(resolve=>release=resolve);
const calls=[],pages=[];
const agents=module('sessions',{gate,calls,pages});
agents.evaluate(`
  globalThis.mounts=0;
  agentController.mount=async(_root,ctx)=>{
    mounts++;
    let query='';
    _toolbar=()=>({search:{getValue:()=>query,setValue:value=>query=value}});
    _open=page=>{pages.push(page);ctx.refreshToolbar();};
    await gate;
  };
`);
const views=Array.from(agents.evaluate('createAgentViews()'));
assert.deepEqual(views.map(view=>view.route),['agents/overview','agents/sessions','agents/trends','agents/configure']);
assert.deepEqual(views[2].aliases,['agents/tools','agents/models'],'old Tools and Models links land on Trends');
const mounts=views.map(view=>view.mount({}, {refreshToolbar:()=>calls.push(view.id)}));
assert.equal(agents.evaluate('mounts'),1);
assert.ok(mounts.every(promise=>promise===mounts[0]),'siblings await the same mounted controller');
release();await Promise.all(mounts);
views[1].show();views[1].toolbar().search.setValue('retained query');
views[2].show();views[1].show();
assert.deepEqual(pages,['sessions','trends','sessions']);
assert.deepEqual(calls,['agents-sessions','agents-trends','agents-sessions'],'old mount context must not own sibling refreshes');
assert.equal(views[1].toolbar().search.getValue(),'retained query');
""")

    def test_inbox_pages_load_independently_and_keep_item_state(self) -> None:
        self.run_probe(r"""
const app=inbox();await app.mount();
assert.equal(app.requests.length,0,'mounting a Jobs deep link must not wait for Items');
assert.equal(app.page('jobs').toolbar(),null,'toolbar belongs to the requested route before show runs');
assert.equal(app.page('connections'),undefined,'Connections is a Setup section, not a Work page');
app.page('items').show();
assert.deepEqual(app.requests.map(request=>request.path),['/api/inbox?section=projects&state=open&limit=200']);
app.requests[0].resolve({ok:true,data:{sections:[{id:'projects',label:'Projects',counts:{new:1,running:0,waiting:0,failed:0,closed:0},entities:[{id:'xo-space',label:'xo-space',counts:{new:1}}]}],
  rows:[{kind:'workitem',id:'one',project_id:'xo-space',title:'Retained item',section:'projects',entity:'xo-space',state:'new',status:'open',updated_at:''}],count:1}});await settle();
const search=app.page('items').toolbar().search;
search.setValue('retained');
const items=app.nodes.get('.inb-items-page'),before=items.innerHTML,paints=items.paints;
app.page('items').hide();app.page('jobs').show();
assert.equal(app.page('jobs').toolbar(),null);
assert.equal(app.nodes.get('.inb-page-head h1').textContent,'Jobs');
assert.equal(items.hidden,true);
assert.equal(app.nodes.get('.inb-jobs-page').hidden,false);
assert.equal(app.intervals.has('inbox-poll'),false);
assert.equal(app.intervals.has('inbox-badge'),true);
assert.equal(app.requests[1].path,'/api/schedules');
app.requests[1].resolve({ok:true,data:{jobs:[{id:'sample',name:'Sample job',every_seconds:60,enabled:true}]}});await settle();
assert.equal(app.evaluate('jobs[0].name'),'Sample job');
assert.equal(items.innerHTML,before);assert.equal(items.paints,paints,'a jobs response must not rebuild item details');
app.page('jobs').hide();app.page('items').show();
assert.equal(search.getValue(),'retained');
assert.equal(items.innerHTML,before);
assert.equal(app.intervals.has('inbox-jobs-poll'),false);
assert.equal(app.intervals.has('inbox-poll'),true);
assert.equal(app.intervals.has('inbox-badge'),false);
app.evaluate("openRow('one','workitem')");
assert.deepEqual(app.opened,['inbox/item?p=xo-space&id=one'],'Open lands on the item page through the hash');
assert.equal(app.events.length,0,'no hand-over event any more');
""")

    def test_jobs_reentry_discards_old_read_and_keeps_results_available(self) -> None:
        self.run_probe(r"""
const app=inbox();await app.mount();
app.page('jobs').show();
assert.equal(app.requests[0].path,'/api/schedules');
app.page('jobs').hide();app.page('items').show();
app.requests[1].resolve({ok:true,data:{sections:[{id:'projects',label:'Projects',counts:{},entities:[]}],rows:[],count:0}});await settle();
const items=app.nodes.get('.inb-items-page'),paints=items.paints;
app.page('items').hide();app.page('jobs').show();
assert.equal(app.requests.length,2,'reentry queues a fresh read behind the outstanding one');
app.requests[0].resolve({ok:true,data:{jobs:[{id:'stale',name:'Stale',every_seconds:60}]}});await settle();
assert.equal(app.requests.length,3);
app.requests[2].resolve({ok:true,data:{jobs:[{id:'fresh',name:'Fresh job',every_seconds:60,enabled:true,running:true}]}});await settle();
assert.equal(app.evaluate('jobs[0].id'),'fresh');
assert.equal(app.intervals.get('inbox-jobs-poll').ms,3000);
assert.equal(app.nodes.get('.inb-page-head h1').textContent,'Jobs');
assert.equal(items.paints,paints);
app.evaluate("onClick({target:{closest:()=>({dataset:{act:'job-results',job:'fresh'}})}})");
assert.equal(app.results[0].id,'fresh');
app.page('jobs').hide();
assert.equal(app.intervals.has('inbox-jobs-poll'),false);
assert.equal(app.intervals.has('inbox-badge'),true);
""")

    def test_jobs_lists_manual_jobs_and_runs_one_now(self) -> None:
        self.run_probe(r"""
const app=inbox();await app.mount();
app.page('jobs').show();
app.requests[0].resolve({ok:true,data:{jobs:[{id:'nightly',name:'Nightly',every_seconds:86400,enabled:true},
  {id:'by-hand',name:'By hand',every_seconds:null,enabled:true}]}});await settle();
assert.deepEqual(Array.from(app.evaluate('jobs.map(job=>job.id)')),['nightly','by-hand'],'manual jobs are listed too');
assert.equal(app.intervals.get('inbox-jobs-poll').ms,30000);
app.evaluate("onClick({target:{closest:()=>({dataset:{act:'job-run',job:'by-hand'}})}})");
const run=app.requests[app.requests.length-1];
assert.equal(run.path,'/api/schedules/by-hand/run');
assert.equal(run.opts.method,'POST');
app.evaluate("onClick({target:{closest:()=>({dataset:{act:'job-run',job:'by-hand'}})}})");
assert.equal(app.requests.length,2,'a second click while the first is in flight sends nothing');
run.resolve({ok:true,data:{ok:true,started:true,job:{id:'by-hand',name:'By hand',every_seconds:null,enabled:true,running:true}}});await settle();
assert.equal(app.evaluate("jobs.find(job=>job.id==='by-hand').running"),true);
assert.equal(app.intervals.get('inbox-jobs-poll').ms,3000,'a started job is polled every 3s');
app.evaluate("onClick({target:{closest:()=>({dataset:{act:'job-run',job:'by-hand'}})}})");
assert.equal(app.requests.length,2,'a running job cannot be started again');
""")


if __name__ == "__main__":
    unittest.main()
