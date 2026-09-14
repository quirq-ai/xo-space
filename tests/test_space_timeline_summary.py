"""Timeline summaries report selected data in the visible date window."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
PRELUDE = r"""
import assert from 'node:assert/strict';
import {timelineSummary} from './space_ui/js/core/timeline-summary.js';
const date=value=>+new Date(value+'T00:00:00');
const options={mode:'file',lanes:['alpha','beta','empty'],totalProjects:3,filtered:false,
  start:date('2025-01-01'),end:date('2025-12-31'),
  files:[{cat:'alpha',date:'2024-12-31'},{cat:'alpha',date:'2025-01-01'},
    {cat:'alpha',date:'2025-12-31'},{cat:'alpha',date:'2026-01-01'},
    {cat:'beta',date:'2025-06-15'},{cat:'beta',date:null},
    {cat:'empty',date:'not-a-date'},{cat:'unselected',date:'2025-06-15'}],
  history:{alpha:[{d:'2024-12-31',n:8},{d:'2025-01-01',n:2},{d:'2025-12-31',n:3},{d:'2026-01-01',n:5}],
    beta:[{d:'2025-06-15',n:4},{d:'not-a-date',n:9},{d:'2025-02-01',n:0},{d:'2025-02-02',n:-1},{d:'2025-02-03',n:Infinity}],
    unselected:[{d:'2025-06-15',n:50}]}};
"""


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class TimelineSummaryTests(unittest.TestCase):
    def probe(self, source):
        result = subprocess.run(['node', '--input-type=module', '-e', PRELUDE+source],
                                cwd=ROOT, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dated_files_include_window_boundaries_and_selected_projects(self):
        self.probe(r"""
const result=timelineSummary(options);
assert.match(result.summary,/^3 dated files · 3 mapped projects ·/);
assert.match(result.summary,/Jan 1, 2025 – Dec 31, 2025/);
assert.match(result.context,/1 project without dated files/);
""")

    def test_commit_totals_count_commits_not_records_or_invalid_values(self):
        self.probe(r"""
const result=timelineSummary({...options,mode:'project'});
assert.match(result.summary,/^9 commits · 3 mapped projects ·/);
assert.match(result.context,/1 project without commit data/);
""")

    def test_filter_counts_selected_projects_against_mapped_total(self):
        self.probe(r"""
const result=timelineSummary({...options,lanes:['alpha'],filtered:true,mode:'project'});
assert.match(result.summary,/^5 commits · 1 of 3 mapped projects ·/);
assert.doesNotMatch(result.context,/without/);
""")

    def test_empty_window_is_not_missing_history(self):
        self.probe(r"""
const result=timelineSummary({...options,lanes:['alpha'],start:date('2027-01-01'),end:date('2027-12-31')});
assert.match(result.summary,/^0 dated files/);
assert.equal(result.context,'No dated files in this date range.');
const missing=timelineSummary({...options,lanes:['empty'],filtered:true});
assert.match(missing.summary,/^0 dated files · 1 of 3 mapped projects/);
assert.equal(missing.context,'No dated files in these mapped projects.');
""")

    def test_empty_selection_and_empty_workspace_have_distinct_guidance(self):
        self.probe(r"""
assert.deepEqual(timelineSummary({...options,lanes:[]}),{
  summary:'No matching projects',context:'Try a different project name.'});
assert.deepEqual(timelineSummary({...options,lanes:[],totalProjects:0}),{
  summary:'No projects mapped',context:'Project dates will appear when Git data is available.'});
""")
