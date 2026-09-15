"""Project pins share browser-local state without requiring a DOM or server."""
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

const source=fs.readFileSync('space_ui/js/core/project-pins.js','utf8')
  .replace(/^import .*;\n/gm,'').replace(/^export /gm,'')
  +'\nglobalThis.pins={isProjectPinned,toggleProjectPin,subscribeProjectPins};';
const makeStore=({apiBase='',origin='https://space.example',saved=null,readError=false,writeError=false,accessError=false}={})=>{
  const key='space.projects.pins.v1:'+String(apiBase||origin||'local');
  const data=new Map(saved===null?[]:[[key,saved]]),writes=[],listeners=new Map(),errors=[];
  const storage={
    getItem(name){if(readError)throw new Error('storage read denied');return data.get(name)??null;},
    setItem(name,value){if(writeError)throw new Error('storage write denied');writes.push([name,value]);data.set(name,value);},
  };
  const context={API_BASE:apiBase,location:{origin},console:{error:(...args)=>errors.push(args)},
    addEventListener:(name,callback)=>listeners.set(name,callback)};
  Object.defineProperty(context,'localStorage',{get(){if(accessError)throw new Error('storage access denied');return storage;}});
  vm.runInNewContext(source,context);
  return {pins:context.pins,key,storage,data,writes,errors,
    setWriteError(value){writeError=value;},
    storageEvent(event){listeners.get('storage')({storageArea:storage,...event});},
  };
};
const plain=value=>JSON.parse(JSON.stringify(value));
"""


@unittest.skipUnless(shutil.which("node"), "node is unavailable")
class ProjectPinsTests(unittest.TestCase):
    def probe(self, source: str) -> None:
        result = subprocess.run(
            ["node", "--input-type=module", "-e", PRELUDE + source],
            cwd=ROOT, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_restores_existing_key_and_saved_id_constraints(self):
        self.probe(r"""
const ids=['keep','',3,null,{},'x'.repeat(201),'y'.repeat(200),...Array.from({length:1100},(_,i)=>'project-'+i)];
const visit=makeStore({apiBase:'http://127.0.0.1:5002',saved:JSON.stringify(ids)});
assert.equal(visit.key,'space.projects.pins.v1:http://127.0.0.1:5002');
for(const id of ['keep','','y'.repeat(200),'project-996'])assert.equal(visit.pins.isProjectPinned(id),true);
for(const id of [3,'x'.repeat(201),'project-997'])assert.equal(visit.pins.isProjectPinned(id),false);
assert.equal(visit.writes.length,0,'Reading existing preferences must not rewrite them');
assert.equal(makeStore().key,'space.projects.pins.v1:https://space.example');
assert.equal(makeStore({origin:''}).key,'space.projects.pins.v1:local');
""")

    def test_local_updates_are_synchronous_shared_and_persisted(self):
        self.probe(r"""
const visit=makeStore(),seen=[],other=[];
const unsubscribe=visit.pins.subscribeProjectPins(event=>seen.push({...event,pinned:visit.pins.isProjectPinned('alpha')}));
visit.pins.subscribeProjectPins(event=>other.push({...event,pinned:visit.pins.isProjectPinned('alpha')}));
assert.equal(seen.length,0,'Subscription must not pretend a change happened');
assert.deepEqual(plain(visit.pins.toggleProjectPin('alpha')),{pinned:true,persisted:true});
assert.deepEqual(seen,[{source:'local',persisted:true,pinned:true}]);
assert.deepEqual(other,seen);
assert.deepEqual(JSON.parse(visit.data.get(visit.key)),['alpha']);
unsubscribe();
assert.deepEqual(plain(visit.pins.toggleProjectPin('alpha')),{pinned:false,persisted:true});
assert.equal(seen.length,1);assert.equal(other.at(-1).pinned,false);
assert.deepEqual(JSON.parse(visit.data.get(visit.key)),[]);
""")

    def test_failed_storage_keeps_visit_state_and_recovers_on_later_write(self):
        self.probe(r"""
const visit=makeStore({saved:'["existing"]',writeError:true}),seen=[];
visit.pins.subscribeProjectPins(event=>seen.push(plain(event)));
assert.deepEqual(plain(visit.pins.toggleProjectPin('new')),{pinned:true,persisted:false});
assert.equal(visit.pins.isProjectPinned('existing'),true);
assert.equal(visit.pins.isProjectPinned('new'),true);
assert.deepEqual(seen,[{source:'local',persisted:false}]);
assert.equal(visit.data.get(visit.key),'["existing"]');
visit.setWriteError(false);
assert.deepEqual(plain(visit.pins.toggleProjectPin('third')),{pinned:true,persisted:true});
assert.deepEqual(JSON.parse(visit.data.get(visit.key)),['existing','new','third']);
for(const options of [{readError:true},{accessError:true},{saved:'broken json'},{saved:'{"alpha":true}'}]){
  const unavailable=makeStore(options);
  assert.equal(unavailable.pins.isProjectPinned('alpha'),false);
  assert.equal(unavailable.pins.toggleProjectPin('alpha').pinned,true);
  assert.equal(unavailable.pins.isProjectPinned('alpha'),true);
}
""")

    def test_cross_tab_updates_and_clear_reach_subscribers_without_writes(self):
        self.probe(r"""
const visit=makeStore({saved:'["old"]'}),seen=[];
visit.pins.subscribeProjectPins(event=>seen.push({...event,old:visit.pins.isProjectPinned('old'),fresh:visit.pins.isProjectPinned('fresh')}));
visit.storageEvent({key:visit.key,newValue:'["fresh"]'});
assert.deepEqual(seen,[{source:'storage',persisted:true,old:false,fresh:true}]);
assert.equal(visit.writes.length,0,'A cross-tab notification must not echo a storage write');
visit.storageEvent({key:visit.key,newValue:null});
assert.equal(visit.pins.isProjectPinned('fresh'),false);
visit.storageEvent({key:visit.key,newValue:'["fresh"]'});
visit.storageEvent({key:null,newValue:null});
assert.equal(visit.pins.isProjectPinned('fresh'),false,'localStorage.clear must clear shared pins');
assert.equal(seen.length,4);
""")

    def test_ignores_unrelated_or_malformed_cross_tab_events(self):
        self.probe(r"""
const visit=makeStore({saved:'["existing"]'}),seen=[];
visit.pins.subscribeProjectPins(event=>seen.push(event));
for(const event of [
  {key:'space.projects.pins.v1:https://other.example',newValue:'[]'},
  {key:visit.key,newValue:'[]',storageArea:{}},
  {key:null,newValue:null,storageArea:{}},
  {key:visit.key,newValue:'malformed'},
  {key:visit.key,newValue:'{"not":"a list"}'},
])visit.storageEvent(event);
assert.equal(visit.pins.isProjectPinned('existing'),true);
assert.equal(seen.length,0);
assert.equal(visit.writes.length,0);
""")

    def test_one_subscriber_cannot_prevent_other_views_updating(self):
        self.probe(r"""
const visit=makeStore(),seen=[];
visit.pins.subscribeProjectPins(()=>{throw new Error('fixture stale view');});
visit.pins.subscribeProjectPins(event=>{event.persisted=false;});
visit.pins.subscribeProjectPins(event=>seen.push(plain(event)));
assert.deepEqual(plain(visit.pins.toggleProjectPin('alpha')),{pinned:true,persisted:true});
assert.deepEqual(seen,[{source:'local',persisted:true}]);
assert.equal(visit.errors.length,1);
""")

    def test_invalid_new_ids_do_not_pollute_saved_preferences(self):
        self.probe(r"""
const visit=makeStore(),seen=[];
visit.pins.subscribeProjectPins(event=>seen.push(event));
for(const id of [null,undefined,12,{},'a'.repeat(201)]){
  assert.deepEqual(plain(visit.pins.toggleProjectPin(id)),{pinned:false,persisted:false});
  assert.equal(visit.pins.isProjectPinned(id),false);
}
assert.equal(visit.writes.length,0);assert.equal(seen.length,0);
""")


if __name__ == "__main__":
    unittest.main()
