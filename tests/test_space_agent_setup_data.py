"""Read-only explorer adapters preserve identity, scope and secret boundaries."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
PRELUDE = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {SETUP_SECTIONS} from './space_ui/js/core/setup-sections.js';
function adapter(name,fetch=async()=>({ok:false})){
  const context={API_BASE:'https://space.example',apiFetch:fetch,SETUP_SECTIONS,AbortController,setTimeout,clearTimeout};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync('space_ui/js/core/read-snapshot.js','utf8')
    .replace(/^import .*;\n/gm,'').replaceAll('export async function','async function'),context);
  vm.runInContext(fs.readFileSync('space_ui/js/views/'+name+'-data.js','utf8')
    .replace(/^import .*;\n/gm,'').replaceAll('export async function','async function').replaceAll('export function','function'),context);
  return source=>vm.runInContext(source,context);
}
const good=data=>({ok:true,data});
const native={github:good({status:'connected',username:'sample'}),magicpath:good({logged_in:false,cli_installed:true,skill_installed:true}),
  vercel:good({status:'needs_auth'}),gdrive:good({remotes:[]}),onedrive:good({remotes:[]})};
"""


@unittest.skipUnless(shutil.which("node"), "node is unavailable")
class SpaceAgentSetupDataTests(unittest.TestCase):
    def probe(self, source):
        result = subprocess.run(["node", "--input-type=module", "-e", PRELUDE + source],
                                cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_agent_identity_tree_scope_and_tokens(self):
        self.probe(r"""
const build=adapter('agent')('buildAgentData');
const result=build({meta:{sources:[{id:'alpha',label:'Alpha',available:true},{id:'beta',label:'Beta',available:true}]},
 totals:{sessions:9,sessions_by_agent:{alpha:8,beta:1}},sessions:[
  {id:'same',key:'alpha:same',agent:'alpha',project:'Demo',project_path:'/one/demo',total_tokens:100,own_tokens:75,
   model:'Actual model',tools:[{name:'Read',calls:2}],subagents:[{id:'same/child',tokens:25}]},
  {id:'other',agent:'alpha',project:'Demo',project_path:'/two/demo',total_tokens:0,subagents:[]},
  {id:'same',key:'beta:same',agent:'beta',project:'Demo',project_path:'/one/demo',cost_known:false,subagents:[]},
 ]});
assert.equal(new Set(result.nodes.map(n=>n.id)).size,result.nodes.length);
assert.equal(result.nodes.filter(n=>n.kind==='project').length,3,'same basename/path in different runtime groups remains distinct');
const sessions=result.nodes.filter(n=>n.kind==='session');assert.equal(sessions.length,3);
const parent=sessions.find(n=>n.meta.some(([k,v])=>k==='Own tokens'&&v==='75'));
assert.ok(parent.meta.some(([k,v])=>k==='Tokens (including listed subagents)'&&v==='100'));
assert.ok(parent.meta.some(([k,v])=>k==='Recorded tools'&&v==='Read (2 calls)'));
assert.equal(result.nodes.find(n=>n.kind==='subagent').parentId,parent.id);
assert.match(result.warning,/capped/);assert.match(result.warning,/2 loaded sessions of 8/);
assert.ok(sessions.some(n=>n.meta.some(([k,v])=>k==='Tokens (including listed subagents)'&&v==='0')));
assert.ok(sessions.some(n=>n.meta.some(([k,v])=>k==='Cost'&&v==='Unavailable')));
for(const edge of result.edges)assert.ok(result.nodes.some(n=>n.id===edge.source)&&result.nodes.some(n=>n.id===edge.target));
assert.equal(build({sessions:[],totals:{sessions:0}}).warning,'');
assert.match(build(null).warning,/Could not load/);
""")

    def test_setup_whitelists_metadata_and_survives_partial_failure(self):
        self.probe(r"""
const build=adapter('setup')('buildSetupData');
const sentinel='DO_NOT_EXPOSE_CREDENTIAL';
const result=build({...native,
 runtime:good({configured:{agent_name:'alpha',env:{TOKEN:sentinel}},applied:{agent_name:'beta',watcher_enabled:false},restart_required:true}),
 identity:good({space:{status:'configured',id:'space-id',owner:'Owner'},xo:{status:'connected',user_id:'user'},github:{status:'not_configured'},token:sentinel}),
 projects:good({items:[{id:'sample',display_name:'Sample',path:'/workspace/sample'}]}),
 connections:{ok:false,error:sentinel},
 secrets:good({items:[{key:'API_TOKEN',is_set:true,value:sentinel,masked_value:sentinel},{key:'EMPTY_KEY',is_set:false}]}),
 jobs:good({jobs:[{id:'manual',name:'Manual',every_seconds:null,enabled:true,command:{argv:[sentinel],env:{TOKEN:sentinel}},last_result:{output_tail:sentinel}},
  {id:'scheduled',name:'Scheduled',every_seconds:60,enabled:false}]}),
 server:good({running:true}),
});
assert.equal(result.nodes.filter(n=>n.kind==='section').length,7);
assert.doesNotMatch(JSON.stringify(result),new RegExp(sentinel));
assert.match(result.warning,/Polled connections unavailable/);
assert.equal(result.nodes.find(n=>n.label==='API_TOKEN').detail,'Configured');
assert.equal(result.nodes.find(n=>n.label==='EMPTY_KEY').detail,'Empty');
assert.equal(result.nodes.find(n=>n.label==='Manual').detail,'Manual command');
assert.equal(result.nodes.find(n=>n.label==='Scheduled').detail,'Every 60 seconds');
assert.equal(result.nodes.find(n=>n.label==='Chat agent').detail,'beta');
assert.equal(result.nodes.find(n=>n.label==='Activity collection').detail,'Disabled');
assert.equal(result.nodes.find(n=>n.label==='Sample').route,'setup/projects');
const unavailable=build({secrets:{ok:true,data:{items:null}}});
assert.equal(unavailable.nodes.filter(n=>n.kind==='section').length,7);
assert.match(unavailable.warning,/Secret names unavailable/);
assert.ok(unavailable.nodes.every(n=>n.route),'failed reads retain usable management navigation');
""")

    def test_loaders_use_only_existing_get_endpoints(self):
        self.probe(r"""
const reads=[];
const fetch=async(path,options)=>{reads.push([path,options]);return{ok:false,error:'PRIVATE_ERROR'};};
await adapter('agent',fetch)('loadAgentData()');
const setup=await adapter('setup',fetch)('loadSetupData()');
assert.equal(reads.length,13);
assert.ok(reads.every(([path,options])=>path.startsWith('https://space.example/')&&options.signal instanceof AbortSignal&&!options.method));
assert.ok(reads.some(([path])=>path==='https://space.example/api/secrets'));
assert.ok(reads.some(([path])=>path==='https://space.example/api/connectors/gdrive/remotes'));
assert.ok(reads.every(([path])=>!/(?:\/run|\/auth)(?:\/|$)/.test(path)));
assert.doesNotMatch(JSON.stringify(setup),/PRIVATE_ERROR/);
""")

    def test_hung_and_throwing_reads_are_bounded_and_preserve_other_scopes(self):
        self.probe(r"""
const aborted=[];
const fetch=async(path,{signal})=>{
 if(path.endsWith('/api/secrets'))return good({items:[{key:'ONLY_KEY',is_set:true}]});
 if(path.endsWith('/status'))throw new Error('PRIVATE_PROVIDER_ERROR');
 signal.addEventListener('abort',()=>aborted.push(path));
 return new Promise(()=>{});
};
const started=Date.now();
const result=await adapter('setup',fetch)('loadSetupData({timeoutMs:10})');
assert.ok(Date.now()-started<500,'a hung native provider must not strand successful scopes');
assert.ok(result.nodes.some(node=>node.label==='ONLY_KEY'));
assert.ok(aborted.length>0,'timed-out reads actively cancel their request');
assert.match(result.warning,/Local projects unavailable/);
assert.doesNotMatch(JSON.stringify(result),/PRIVATE_PROVIDER_ERROR/);
const agent=await adapter('agent',fetch)('loadAgentData({timeoutMs:10})');
assert.match(agent.warning,/Could not load agent sessions/);
assert.ok(aborted.some(path=>path.endsWith('/xo/sessions.json')));
""")

    def test_api_forwards_owned_signals_without_cancelling_shared_reads(self):
        self.probe(r"""
const calls=[];let shared=0;
const context={location:{pathname:'/space/',search:''},TypeError,
 singleFlight:(_key,run)=>{shared++;return run();},
 fetch:async(path,options)=>{calls.push({path,options});return{ok:true,status:200,json:async()=>({})};}};
vm.createContext(context);
vm.runInContext(fs.readFileSync('space_ui/js/core/api.js','utf8').replace(/^import .*;\n/gm,'').replaceAll('export const','const').replaceAll('export function','function'),context);
const fetch=vm.runInContext('apiFetch',context),controller=new AbortController();
await fetch('/api/schedules');await fetch('/api/schedules',{signal:controller.signal});
assert.equal(shared,1,'ordinary GETs stay shared but cancellable GETs have their own request');
assert.equal(calls[1].options.signal,controller.signal);assert.equal(calls[1].options.method,'GET');
""")


if __name__ == "__main__":
    unittest.main()
