"""Links, FIFOs and devices at state paths are reported, never opened.

Live test D4: a FIFO at inbox/inbox.json and scheduler/jobs.json -> /dev/zero
were reported healthy, while the server spent about 1 GB per tick on the latter.
"""

from __future__ import annotations

import json
import os

from tests.doctor_sandbox import DoctorSandbox


class SpecialEntryTests(DoctorSandbox):
    def at(self, subject: str) -> list[dict]:
        return [f for c in self.report()["checks"] for f in c["findings"] if f["subject"] == subject]

    def replace_with_link(self, rel: str, target) -> None:
        path = self.state / rel
        path.unlink()
        path.symlink_to(target)

    def test_a_fifo_is_reported_and_not_opened(self) -> None:
        inbox = self.state / "inbox" / "inbox.json"
        inbox.unlink()
        os.mkfifo(inbox)
        [finding] = self.at("inbox/inbox.json")
        self.assertEqual((finding["id"], finding["level"]), ("read.special", "FAIL"))
        self.assertIn("a pipe", finding["observed"])

    def test_a_link_to_a_device_names_the_link(self) -> None:
        self.replace_with_link("scheduler/jobs.json", "/dev/zero")
        [finding] = self.at("scheduler/jobs.json")
        self.assertEqual((finding["id"], finding["level"]), ("read.special", "FAIL"))
        self.assertIn({"label": "Link to", "value": "/dev/zero"}, finding["evidence"])

    def test_a_dangling_link_to_a_file_whose_loss_is_a_fail(self) -> None:
        self.replace_with_link("inbox/inbox.json", self.state / "nowhere" / "inbox.json")
        [finding] = self.at("inbox/inbox.json")
        self.assertEqual((finding["id"], finding["level"]), ("shape.symlink", "FAIL"))
        self.assertEqual(finding["title"], "The Inbox is a link to something that isn't there")
        self.assertEqual(finding["problem_key"], "file:inbox/inbox.json:link")

    def test_a_dangling_link_to_a_rebuilt_file_is_a_warning(self) -> None:
        self.replace_with_link("cache/stats.json", "/nonexistent/stats.json")
        [finding] = self.at("cache/stats.json")
        self.assertEqual((finding["id"], finding["level"]), ("shape.symlink", "WARN"))

    def test_a_link_to_a_good_file_warns_that_saves_replace_it(self) -> None:
        elsewhere = self.state.parent / "elsewhere-inbox.json"
        elsewhere.write_text((self.state / "inbox" / "inbox.json").read_text(encoding="utf-8"), encoding="utf-8")
        self.replace_with_link("inbox/inbox.json", elsewhere)
        [finding] = self.at("inbox/inbox.json")
        self.assertEqual((finding["id"], finding["level"]), ("shape.symlink", "WARN"))
        self.assertIn("each save replaces the link", finding["consequence"])

    def test_a_link_to_a_damaged_file_reports_both(self) -> None:
        elsewhere = self.state.parent / "broken.json"
        elsewhere.write_text("{", encoding="utf-8")
        self.replace_with_link("scheduler/jobs.json", elsewhere)
        ids = sorted(f["id"] for f in self.at("scheduler/jobs.json"))
        self.assertEqual(ids, ["read.invalid_json", "shape.symlink"])

    def test_an_unknown_fifo_is_only_a_note(self) -> None:
        os.mkfifo(self.state / "cache" / "mystery")
        [finding] = self.at("cache/mystery")
        self.assertEqual((finding["id"], finding["level"]), ("inventory.unknown_file", "OK"))

    def test_logs_and_credentials_behind_links_are_left_alone(self) -> None:
        self.replace_with_link("logs/quirq.log", "/dev/null")
        self.assertEqual(self.at("logs/quirq.log"), [])
        self.assertEqual([f["id"] for f in self.problems()], [])

    def test_a_link_to_a_folder_is_not_entered(self) -> None:
        outside = self.state.parent / "outside"
        (outside / "deep").mkdir(parents=True)
        (outside / "deep" / "inbox.json").write_text(json.dumps([]), encoding="utf-8")
        (self.state / "cache" / "linked").symlink_to(outside, target_is_directory=True)
        self.assertEqual([f["id"] for f in self.problems()], [])
