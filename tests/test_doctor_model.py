"""The report's finding shape: v1 fields unchanged, #188 fields added."""

from __future__ import annotations

import unittest

from services.doctor.model import FAIL, Finding, compose_why, ev, moment
from tests.doctor_sandbox import DoctorSandbox


class FindingShapeTests(unittest.TestCase):
    def test_a_v1_finding_serialises_as_before_plus_problem_key(self) -> None:
        finding = Finding("read.empty", FAIL, "inbox/inbox.json", "/s/inbox/inbox.json",
                          "The file is empty.", "It matters.")
        out = finding.to_dict()
        self.assertEqual(out["why_it_matters"], "It matters.")
        self.assertEqual(out["problem_key"], "read.empty:inbox/inbox.json")
        for absent in ("title", "evidence", "consequence", "self_repair", "next_step", "related", "action"):
            self.assertNotIn(absent, out)

    def test_parts_compose_why_it_matters_when_it_is_empty(self) -> None:
        finding = Finding("x.y", FAIL, "s", "", "Observed.", "", consequence="Stops.",
                          self_repair="Nothing.", next_step="Do this.", title="The thing is broken",
                          evidence=[ev("Size", 12)], problem_key="file:s")
        out = finding.to_dict()
        self.assertEqual(out["why_it_matters"], "Stops. Nothing. Do this.")
        self.assertEqual(out["title"], "The thing is broken")
        self.assertEqual(out["evidence"], [{"label": "Size", "value": "12"}])
        self.assertEqual(out["problem_key"], "file:s")

    def test_an_explicit_why_is_kept(self) -> None:
        finding = Finding("x.y", FAIL, "s", "", "o", "Explicit.", consequence="Other.")
        self.assertEqual(finding.why_it_matters, "Explicit.")

    def test_compose_skips_blank_parts(self) -> None:
        self.assertEqual(compose_why("A.", "", "  ", "B."), "A. B.")

    def test_moment_is_iso_and_relative_and_never_negative(self) -> None:
        self.assertEqual(moment(1_000_000_000, 1_000_000_000 + 180), "2001-09-09T01:46:40Z (3 minutes ago)")
        self.assertEqual(moment(1_000_000_300, 1_000_000_000), "2001-09-09T01:51:40Z (in the future)")


class FindingCountsTests(DoctorSandbox):
    def test_the_report_counts_findings_not_checks(self) -> None:
        for name in ("inbox/inbox.json", "scheduler/jobs.json"):
            (self.state / name).write_text("", encoding="utf-8")
        report = self.report()
        self.assertEqual(set(report["finding_counts"]), {"OK", "WARN", "FAIL", "ERROR"})
        top = [f for c in report["checks"] for f in c["findings"]]
        self.assertEqual(sum(report["finding_counts"].values()), len(top))
        self.assertEqual(report["summary"]["FAIL"], 1)  # summary still counts checks


if __name__ == "__main__":
    unittest.main()
