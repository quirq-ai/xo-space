"""Behavioral probes for shared explorer query and hierarchy handling."""
import pathlib
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class SpaceDataExplorerTests(unittest.TestCase):
    def test_search_keeps_only_matching_records_and_their_ancestors(self):
        source = (ROOT / 'space_ui/js/views/data-explorer.js').read_text()
        source = '\n'.join(line for line in source.splitlines() if not line.startswith('import '))
        source = source.replace('export function ', 'function ')
        source = 'const loadAgentData=()=>{},loadInboxData=()=>{},loadSetupData=()=>{};\n' + source
        probe = r'''
const assert=require('node:assert/strict');
const nodes=[
 {id:'root',label:'Setup',kind:'root'},
 {id:'secrets',parentId:'root',label:'Secrets',kind:'section'},
 {id:'key',parentId:'secrets',label:'DEMO_API_KEY',kind:'secret',meta:[['Configured','Yes']]},
 {id:'projects',parentId:'root',label:'Projects',kind:'section'},
 {id:'repo',parentId:'projects',label:'Aurora',kind:'project',detail:'/workspace/aurora'},
];
let result=visibleRecords(nodes,'DEMO yes');
assert.deepEqual(result.nodes.map(node=>node.id),['root','secrets','key']);
assert.deepEqual([...result.matches],['key']);
assert.equal(visibleRecords(nodes,'missing').nodes.length,0);
assert.equal(visibleRecords(nodes,'').nodes.length,nodes.length);
assert.deepEqual(visibleRecords(nodes,'WORKSPACE AURORA').nodes.map(node=>node.id),['root','projects','repo']);
// A malformed parent cycle must not hang filtering or discard the match.
const cyclic=[{id:'a',parentId:'b',label:'needle'},{id:'b',parentId:'a',label:'parent'}];
assert.deepEqual(visibleRecords(cyclic,'needle').nodes.map(node=>node.id),['a','b']);
assert.deepEqual(visibleRecords([{id:'x',parentId:'gone',label:'needle'}],'needle').nodes.map(node=>node.id),['x']);
'''
        result = subprocess.run(['node', '-e', source + '\n' + probe], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
