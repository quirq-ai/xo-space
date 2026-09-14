"""Inbox Graph/Tree reads expose real, bounded relationships and no action data."""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOT = r'''
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const reads=[];
let reply=async()=>({ok:false});
const context={API_BASE:'',setTimeout,clearTimeout,AbortController,apiFetch:(path,options)=>{
  assert.equal(options?.method,undefined,'representations only use GETs');
  assert(options.signal instanceof AbortSignal,'composite reads are independently cancellable');
  reads.push(path);return reply(path);
}};
vm.createContext(context);
for(const file of ['space_ui/js/core/connections.js','space_ui/js/core/read-snapshot.js','space_ui/js/views/inbox-data.js']){
  const source=fs.readFileSync(file,'utf8').replace(/^import .*?;\n/gm,'').replaceAll('export ','');
  vm.runInContext(source,context);
}
const build=input=>JSON.parse(JSON.stringify(context.buildInboxData(input)));
'''


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class InboxDataRepresentationTests(unittest.TestCase):
    def probe(self, source: str) -> None:
        result = subprocess.run(
            ['node', '--input-type=module', '-e', BOOT + source], cwd=ROOT,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_relationships_identifiers_metadata_and_summary_field_boundary(self) -> None:
        self.probe(r'''
const data=build({
  inbox:{counts:{new:1,seen:1,done:0},items:[
    {id:'same',title:'Review <script>',source:'issues',kind:'issue',project_id:'A:B',status:'new',body:'private-body'},
    {id:'other',title:'Local event',source:'issues',status:'seen'},
    null,{title:'Missing identity'},
  ]},
  connections:{connections:[{toolkit:'same',display_name:'GitHub',configured:true,enabled:true,
    interval_s:900,account_label:'dev@example.com',collectors:['issues','issues'],
    available_collectors:[{id:'issues',label:'GitHub issues'}],token:'private-token',
    last_error:'provider-error-with-secret'}, {toolkit:'unused',configured:false,connected_here:false}]},
  schedules:{jobs:[{id:'same',name:'Mirror',every_seconds:60,enabled:false,running:false,
    argv:['private-command'],last_result:{status:'ok',duration_seconds:2,output_tail:'private-output'}},
    {id:'manual',name:'Manual only',every_seconds:null}]},
});
const byId=new Map(data.nodes.map(node=>[node.id,node]));
assert.equal(byId.size,data.nodes.length,'same identifiers in different scopes cannot collide');
assert.equal(data.nodes.filter(node=>node.kind==='collector').length,1,'duplicate collector identities collapse');
for(const node of data.nodes)if(node.parentId)assert(byId.has(node.parentId),'every parent is present');
for(const edge of data.edges){assert(byId.has(edge.source));assert(byId.has(edge.target));}
assert.equal(data.edges.length,data.nodes.length-1,'all relationships form the represented hierarchy');
const item=data.nodes.find(node=>node.kind==='item'&&node.label==='Review <script>');
assert.equal(byId.get(item.parentId).label,'A:B');
assert.equal(byId.get(byId.get(item.parentId).parentId).label,'issues');
assert.equal(item.route,'inbox/items');
const unassigned=data.nodes.find(node=>node.label==='Local event');
assert.equal(byId.get(unassigned.parentId).kind,'source','missing project does not invent a project relationship');
const conn=data.nodes.find(node=>node.kind==='connection');
assert(conn.meta.some(([key,value])=>key==='Interval'&&value==='Every 15 min'));
assert(conn.meta.some(([key,value])=>key==='Last poll state'&&value==='Failed'));
assert(conn.meta.some(([key,value])=>key==='Account'&&value==='dev@example.com'));
assert.equal(conn.route,'inbox/connections');
const job=data.nodes.find(node=>node.kind==='job');
assert(job.meta.some(([key,value])=>key==='Schedule'&&value==='Disabled'));
assert(job.meta.some(([key,value])=>key==='Run state'&&value==='ok'));
assert.equal(byId.get(job.parentId).label,'Every 1 min');
assert.equal(job.route,'inbox/jobs');
const serialized=JSON.stringify(data);
for(const secret of ['private-body','private-token','provider-error-with-secret','private-command','private-output'])assert(!serialized.includes(secret));
assert(!data.nodes.some(node=>node.label==='Manual only'||node.label==='unused'));
assert.equal(data.warning,'');
''')

    def test_empty_unavailable_and_capped_data_are_distinct(self) -> None:
        self.probe(r'''
const empty=build({inbox:{items:[],counts:{}},connections:{connections:[]},schedules:{jobs:[]}});
assert.equal(empty.nodes.length,4,'empty scopes remain navigable');
assert(empty.nodes.filter(node=>node.kind==='scope').every(node=>node.meta.some(([key,value])=>key==='State'&&value==='Loaded')));
assert.equal(empty.warning,'');
const partial=build({inbox:{items:[{id:'latest',title:'Latest'}],counts:{new:100,seen:100,done:40}},
  schedules:{jobs:[]},unavailable:['connections']});
assert.match(partial.warning,/Could not load connections/);
assert.match(partial.warning,/newest 1 of 240 inbox items/);
const connection=partial.nodes.find(node=>node.label==='Connections');
assert(connection.meta.some(([key,value])=>key==='State'&&value==='Unavailable'));
assert.match(connection.detail,/Could not load/);
''')

    def test_independent_gets_timeout_and_malformed_reply_preserve_successful_scopes(self) -> None:
        self.probe(r'''
reply=async path=>{
  if(path.includes('/api/inbox?'))return {ok:true,data:{items:[{id:'loaded',title:'Kept item'}]}};
  if(path==='/api/connections')return new Promise(()=>{});
  return {ok:true,data:{jobs:'unexpected-shape',secret:'private-error'}};
};
const data=await context.loadInboxData({timeoutMs:15});
assert.deepEqual(reads,['/api/inbox?status=all&limit=200','/api/connections','/api/schedules']);
assert(data.nodes.some(node=>node.label==='Kept item'));
assert.match(data.warning,/Could not load connections, jobs/);
assert(!JSON.stringify(data).includes('private-error'));
reply=async()=>{throw new Error('private-network-error');};
const failed=await context.loadInboxData({timeoutMs:15});
assert.match(failed.warning,/Could not load items, connections, jobs/);
assert(!JSON.stringify(failed).includes('private-network-error'));
''')
