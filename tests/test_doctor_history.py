"""History: the Space timeline and project timelines (#188 issue 6)."""

from __future__ import annotations

import json
from unittest.mock import patch

from tests.doctor_sandbox import PID, DoctorSandbox

GOOD = json.dumps({"ts": "2026-01-01T09:00:00.000Z", "type": "session.started", "pid": PID,
                   "project_id": "sample-project"})


class HistoryTests(DoctorSandbox):
    def of(self, prefix: str) -> list[dict]:
        return [f for f in self.problems() if f["id"].startswith(prefix)]

    def test_an_emptied_space_timeline_is_reported(self) -> None:
        (self.state / "projects" / "timeline.jsonl").write_text("", encoding="utf-8")
        [finding] = self.of("history.")
        self.assertEqual((finding["id"], finding["level"]), ("history.empty", "WARN"))
        self.assertIn("Inbox", finding["consequence"])

    def test_an_absent_space_timeline_is_fine(self) -> None:
        (self.state / "projects" / "timeline.jsonl").unlink()
        self.assertEqual(self.of("history."), [])

    def test_damaged_lines_are_counted_never_quoted(self) -> None:
        body = "\n".join([GOOD, GOOD, GOOD, "{not json at all", "\x00\x00\x00 secret-ish"]) + "\n"
        (self.state / "projects" / "timeline.jsonl").write_text(body, encoding="utf-8")
        [finding] = self.of("history.invalid_lines")
        self.assertIn("2 of 5 lines", finding["observed"])
        self.assertIn("line 4", finding["observed"])
        self.assertNotIn("secret-ish", json.dumps(finding))

    def test_a_line_still_being_written_is_not_damage(self) -> None:
        (self.state / "projects" / "timeline.jsonl").write_text(GOOD + "\n" + '{"ts": "2026', encoding="utf-8")
        self.assertEqual(self.of("history."), [])

    def test_a_project_timeline_names_its_project(self) -> None:
        (self.state / "projects" / PID / "timeline.jsonl").write_text(GOOD + "\n[1,2]\n", encoding="utf-8")
        [finding] = self.of("history.invalid_lines")
        self.assertEqual(finding["subject"], f"projects/{PID}/timeline.jsonl")
        self.assertIn("sample-project", finding["title"])

    def test_only_the_tail_is_read_and_the_report_says_so(self) -> None:
        body = ("x" * 50 + "\n") * 40 + GOOD + "\n{bad\n"
        (self.state / "projects" / "timeline.jsonl").write_text(body, encoding="utf-8")
        with patch("services.doctor.history.TAIL_BYTES", 200):
            [finding] = self.of("history.invalid_lines")
        self.assertIn("of the last", finding["observed"])
