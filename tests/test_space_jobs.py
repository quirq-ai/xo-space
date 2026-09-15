"""Space UI jobs: plain-language schedules map onto the scheduler's fields."""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRELUDE = r"""
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const jobs=await import(pathToFileURL(process.cwd()+'/space_ui/js/core/jobs.js'));
const {scheduleToFields,jobToSchedule,describeSchedule,splitDuration,statusText,isScheduled}=jobs;
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SpaceJobsTests(unittest.TestCase):
    def run_probe(self, source: str, tz: str) -> None:
        result = subprocess.run(
            ["node", "--input-type=module", "-e", PRELUDE + source],
            cwd=ROOT, capture_output=True, text=True, timeout=20,
            env={**os.environ, "TZ": tz},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_presets_anchor_at_the_next_local_occurrence(self) -> None:
        # 2026-09-16 is a Wednesday; Kolkata has no daylight saving.
        self.run_probe(r"""
const now=new Date('2026-09-16T10:00:00+05:30');
const at=choice=>scheduleToFields(choice,now);
assert.deepEqual(at({kind:'daily',time:'02:00'}),{every_seconds:86400,first_run_at:'2026-09-17T02:00:00+05:30'});
assert.deepEqual(at({kind:'daily',time:'11:30'}),{every_seconds:86400,first_run_at:'2026-09-16T11:30:00+05:30'});
assert.equal(at({kind:'daily',time:'10:00'}).first_run_at,'2026-09-17T10:00:00+05:30','now itself is not next');
assert.deepEqual(at({kind:'hourly',minute:'15'}),{every_seconds:3600,first_run_at:'2026-09-16T10:15:00+05:30'});
assert.equal(at({kind:'hourly',minute:'0'}).first_run_at,'2026-09-16T11:00:00+05:30');
assert.deepEqual(at({kind:'weekly',weekday:'1',time:'09:00'}),{every_seconds:604800,first_run_at:'2026-09-21T09:00:00+05:30'});
assert.equal(at({kind:'weekly',weekday:'3',time:'09:00'}).first_run_at,'2026-09-23T09:00:00+05:30','earlier today moves a week');
assert.equal(at({kind:'weekly',weekday:'3',time:'11:00'}).first_run_at,'2026-09-16T11:00:00+05:30');
assert.equal(at({kind:'weekly',weekday:'6',time:'23:59'}).first_run_at,'2026-09-19T23:59:00+05:30');
assert.equal(new Date(at({kind:'daily',time:'23:30'}).first_run_at).getTime(),new Date('2026-09-16T18:00:00Z').getTime());
""", "Asia/Kolkata")

    def test_custom_intervals_and_plain_errors(self) -> None:
        self.run_probe(r"""
const now=new Date('2026-09-16T10:00:00+05:30');
assert.deepEqual(scheduleToFields({kind:'custom',every:'30',unit:'minutes'},now),{every_seconds:1800,first_run_at:null});
assert.deepEqual(scheduleToFields({kind:'custom',every:'2',unit:'days',anchor:'2026-09-01T00:00:00Z'},now),
  {every_seconds:172800,first_run_at:'2026-09-01T00:00:00Z'},'an edited job keeps its grid');
for(const bad of [{kind:'custom',every:'',unit:'minutes'},{kind:'custom',every:'1.5',unit:'hours'},{kind:'custom',every:'0',unit:'hours'},
  {kind:'custom',every:'5',unit:'weeks'},{kind:'hourly',minute:''},{kind:'hourly',minute:'60'},{kind:'daily',time:''},
  {kind:'daily',time:'25:00'},{kind:'weekly',weekday:'7',time:'09:00'},{}]){
  const out=scheduleToFields(bad,now);
  assert.equal(typeof out.error,'string',JSON.stringify(bad));
  assert.equal(out.every_seconds,undefined);
}
""", "Asia/Kolkata")

    def test_saved_jobs_reopen_as_the_choice_that_made_them(self) -> None:
        self.run_probe(r"""
assert.equal(jobToSchedule({every_seconds:null}),null);
assert.equal(isScheduled({every_seconds:null}),false);
assert.equal(isScheduled({every_seconds:60}),true);
assert.deepEqual(jobToSchedule({every_seconds:86400,first_run_at:'2026-09-16T20:30:00Z'}),{kind:'daily',time:'02:00'});
assert.deepEqual(jobToSchedule({every_seconds:604800,next_run:'2026-09-21T03:30:00Z',first_run_at:null}),{kind:'weekly',weekday:1,time:'09:00'});
assert.deepEqual(jobToSchedule({every_seconds:3600,first_run_at:'2026-09-16T04:45:00Z'}),{kind:'hourly',minute:15});
assert.deepEqual(jobToSchedule({every_seconds:3600,next_run:'2026-09-16T04:45:27Z'}),{kind:'custom',every:1,unit:'hours',anchor:null},
  'an unanchored slot off the minute is not an hourly preset');
assert.deepEqual(jobToSchedule({every_seconds:90}),{kind:'custom',every:90,unit:'seconds',anchor:null});
assert.deepEqual(jobToSchedule({every_seconds:7200,first_run_at:'2026-09-16T00:00:00Z'}),{kind:'custom',every:2,unit:'hours',anchor:'2026-09-16T00:00:00Z'});
assert.equal(describeSchedule({every_seconds:null}),'Manual');
assert.equal(describeSchedule({every_seconds:86400,first_run_at:'2026-09-16T20:30:00Z'}),'Every day at 02:00');
assert.equal(describeSchedule({every_seconds:604800,first_run_at:'2026-09-21T03:30:00Z'}),'Every Monday at 09:00');
assert.equal(describeSchedule({every_seconds:3600,first_run_at:'2026-09-16T04:45:00Z'}),'Every hour at :15');
assert.equal(describeSchedule({every_seconds:1800}),'Every 30 minutes');
assert.equal(describeSchedule({every_seconds:60}),'Every minute');
assert.equal(describeSchedule({every_seconds:86400}),'Every day');
assert.equal(describeSchedule({every_seconds:90}),'Every 90 seconds');
assert.deepEqual(splitDuration(300,['hours','minutes','seconds']),{value:5,unit:'minutes'});
assert.deepEqual(splitDuration(30,['hours','minutes','seconds']),{value:30,unit:'seconds'});
assert.deepEqual(splitDuration(0.5,['hours','minutes','seconds']),{value:0.5,unit:'seconds'});
assert.equal(statusText('ok'),'Succeeded');
assert.equal(statusText('timed_out'),'Timed out');
assert.equal(statusText('something_new'),'something_new');
""", "Asia/Kolkata")

    def test_an_anchor_across_a_daylight_saving_change_uses_that_dates_offset(self) -> None:
        # New York falls back on 2026-11-01: 02:00 that morning is EST (-05:00).
        self.run_probe(r"""
const now=new Date('2026-11-01T00:30:00-04:00');
assert.equal(scheduleToFields({kind:'daily',time:'02:00'},now).first_run_at,'2026-11-01T02:00:00-05:00');
assert.deepEqual(jobToSchedule({every_seconds:86400,first_run_at:'2026-10-01T06:00:00Z',next_run:'2026-11-02T06:00:00Z'}),
  {kind:'daily',time:'01:00'},'the upcoming slot decides the local time shown');
""", "America/New_York")


if __name__ == "__main__":
    unittest.main()
