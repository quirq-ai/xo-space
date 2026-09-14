"""Indexed counts stay truthful when the graph omits scanned files."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.visualizer import space_index


ROOT = Path(__file__).resolve().parents[1]


class SpaceIndexCountsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.projects: list[dict] = []
        for name, value in (
            ("xo_projects_root", lambda: self.root),
            ("project_dir", lambda name: self.root / name),
            ("list_projects", lambda: self.projects),
            ("_git_facts", lambda _path: ({}, None, [], [])),
            ("_build_ties", lambda *_args: []),
        ):
            self.stack.enter_context(patch.object(space_index, name, value))

    def project(self, name: str, files: tuple[str, ...] = ()) -> Path:
        folder = self.root / name
        folder.mkdir()
        self.projects.append({"name": name})
        for relative in files:
            path = folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture", encoding="utf-8")
        return folder

    def test_display_caps_keep_omitted_project_counts_and_true_zero(self) -> None:
        self.project("alpha", ("a.txt", "b.txt", "c.txt"))
        self.project("beta", ("src/one.txt", "src/nested/two.txt"))
        self.project("empty")
        with patch.object(space_index, "MAX_LEAVES_PER_PROJECT", 2), patch.object(
            space_index, "MAX_TOTAL_LEAVES", 2
        ):
            data = space_index.build_space_data()
        self.assertEqual(len(data["leaves"]), 2)
        self.assertTrue(all(leaf["path"].startswith("alpha/") for leaf in data["leaves"]))
        counts = {hub["id"]: hub["index_counts"] for hub in data["hubs"]}
        self.assertEqual(counts["p_alpha"], {"files": 3, "folders": 0, "capped": False})
        self.assertEqual(counts["p_beta"], {"files": 2, "folders": 2, "capped": False})
        self.assertEqual(counts["p_empty"], {"files": 0, "folders": 0, "capped": False})
        self.assertTrue(data["meta"]["index_counts_complete"])

    def test_scan_limit_is_a_lower_bound_but_exact_limit_can_be_complete(self) -> None:
        self.project("more", ("a.txt", "b.txt", "c.txt"))
        self.project("exact", ("a.txt", "b.txt"))
        with patch.object(space_index, "MAX_FILES_SCANNED_PER_PROJECT", 2):
            data = space_index.build_space_data()
        counts = {hub["id"]: hub["index_counts"] for hub in data["hubs"]}
        self.assertEqual(counts["p_more"], {"files": 2, "folders": 0, "capped": True})
        self.assertEqual(counts["p_exact"], {"files": 2, "folders": 0, "capped": False})
        self.assertFalse(data["meta"]["index_counts_complete"])

    def test_unreadable_subtree_is_not_reported_as_complete_empty_scan(self) -> None:
        folder = self.project("unreadable")
        (folder / "locked").mkdir()

        def unreadable(_base, *, onerror):
            onerror(PermissionError("fixture subtree"))
            return iter(())

        with patch.object(space_index.os, "walk", unreadable):
            data = space_index.build_space_data()
        self.assertEqual(
            data["hubs"][0]["index_counts"], {"files": 0, "folders": 0, "capped": True}
        )
        self.assertFalse(data["meta"]["index_counts_complete"])

    def test_deadline_and_failed_project_leave_unknown_counts(self) -> None:
        self.project("skipped", ("a.txt",))
        with patch.object(space_index.time, "monotonic", side_effect=[0, 100]):
            data = space_index.build_space_data()
        self.assertEqual(data["hubs"], [])
        self.assertFalse(data["meta"]["index_counts_complete"])
        with patch.object(space_index, "_walk_project", side_effect=PermissionError("fixture")):
            data = space_index.build_space_data()
        self.assertEqual(data["hubs"], [])
        self.assertFalse(data["meta"]["index_counts_complete"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class WorkspaceCountsClientTests(unittest.TestCase):
    def run_probe(self, source: str) -> None:
        prelude = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
let response;
globalThis.countsFetch=async()=>response;
const source=fs.readFileSync('space_ui/js/core/workspace.js','utf8')
  .replace("import {API_BASE,apiFetch} from './api.js';",
    "const API_BASE=''; const apiFetch=globalThis.countsFetch;");
const {workspaceCounts}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const load=async data=>{response={ok:true,data};return workspaceCounts();};
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", prelude + source],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_new_metadata_preserves_full_omitted_project_counts_and_empty(self) -> None:
        self.run_probe(r"""
const result=await load({meta:{index_counts_complete:true},hubs:[
  {id:'p_shown',index_counts:{files:600,folders:4,capped:false}},
  {id:'p_omitted',index_counts:{files:200,folders:2,capped:false}},
  {id:'p_empty',index_counts:{files:0,folders:0,capped:false}},
],leaves:[{path:'shown/one.txt'}]});
assert.deepEqual(result.byProject.get('omitted'),{files:200,folders:2,capped:false,known:true});
assert.deepEqual(result.byProject.get('empty'),{files:0,folders:0,capped:false,known:true});
assert.deepEqual(result.totals,{projects:3,files:800,folders:6});
assert.equal(result.totalsCapped,false);
assert.equal(result.byProject.has('not_scanned'),false);
""")

    def test_partial_scan_and_skipped_build_mark_totals_incomplete(self) -> None:
        self.run_probe(r"""
const result=await load({meta:{index_counts_complete:false},hubs:[
  {id:'p_partial',index_counts:{files:2000,folders:1,capped:true}},
],leaves:[]});
assert.deepEqual(result.byProject.get('partial'),{files:2000,folders:1,capped:true,known:true});
assert.equal(result.totalsCapped,true);
const skipped=await load({meta:{index_counts_complete:false},hubs:[],leaves:[]});
assert.equal(skipped.byProject.size,0);
assert.equal(skipped.totalsCapped,true);
""")

    def test_old_global_cap_keeps_positive_lower_bounds_but_zero_unknown(self) -> None:
        self.run_probe(r"""
const result=await load({hubs:[{id:'p_omitted'},{id:'p_partial'},{id:'p_other'}],leaves:[
  ...Array.from({length:20},(_,i)=>({path:'partial/f'+i})),
  ...Array.from({length:1480},(_,i)=>({path:'other/f'+i})),
]});
assert.deepEqual(result.byProject.get('omitted'),{files:0,folders:0,capped:true,known:false});
assert.deepEqual(result.byProject.get('partial'),{files:20,folders:0,capped:true,known:true});
assert.equal(result.totalsCapped,true);
const uncapped=await load({hubs:[{id:'p_empty'},{id:'p_small'}],leaves:[{path:'small/src/one.txt'}]});
assert.deepEqual(uncapped.byProject.get('empty'),{files:0,folders:0,capped:false,known:true});
assert.deepEqual(uncapped.byProject.get('small'),{files:1,folders:1,capped:false,known:true});
""")

    def test_failed_graph_does_not_invent_empty_projects(self) -> None:
        self.run_probe(r"""
response={ok:false,error:'unavailable',offline:true};
const result=await workspaceCounts();
assert.equal(result.ok,false);
assert.equal(result.byProject.size,0);
assert.equal(result.offline,true);
""")

    def test_invalid_new_count_metadata_is_unknown_not_empty(self) -> None:
        self.run_probe(r"""
const result=await load({meta:{index_counts_complete:true},hubs:[
  {id:'p_negative',index_counts:{files:-1,folders:0,capped:false}},
  {id:'p_wrong_type',index_counts:{files:0,folders:0,capped:'false'}},
],leaves:[]});
assert.equal(result.byProject.get('negative').known,false);
assert.equal(result.byProject.get('wrong_type').known,false);
assert.equal(result.totalsCapped,true);
""")


if __name__ == "__main__":
    unittest.main()
