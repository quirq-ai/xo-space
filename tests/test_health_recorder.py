"""services.health.recorder: one record per failure, coalesced, capped, private."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from services.health import recorder


def _boom(message: str = "boom") -> BaseException:
    try:
        raise RuntimeError(message)
    except RuntimeError as exc:
        return exc


def _other_boom() -> BaseException:
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        return exc


class RecorderSandbox(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(self.state),
                                      "XO_PROJECTS_ROOT": str(Path(tmp.name) / "projects")})
        env.start()
        self.addCleanup(env.stop)
        recorder._reset_for_tests()
        self.addCleanup(recorder._reset_for_tests)

    def events(self) -> list[dict]:
        folder = self.state / "setup" / "health" / "events"
        return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))] if folder.exists() else []


class RecordTests(RecorderSandbox):
    def test_the_first_occurrence_is_written_at_once(self) -> None:
        recorder.set_boot_id("boot-1")
        recorder.record("watcher", recorder.CRASH, exc=_boom())
        [event] = self.events()
        self.assertEqual((event["schema"], event["component"], event["kind"], event["error_type"], event["count"]),
                         (1, "watcher", "crash", "RuntimeError", 1))
        self.assertEqual(event["message"], "boom")
        self.assertEqual(event["frames"][-1]["file"], "tests/test_health_recorder.py")
        self.assertEqual(event["frames"][-1]["function"], "_boom")
        self.assertEqual(event["occurrences"][0]["boot_id"], "boot-1")
        self.assertTrue(event["first_seen"].endswith("Z") and event["last_seen"].endswith("Z"))

    def test_the_same_failure_is_one_record_and_a_different_place_is_another(self) -> None:
        recorder.record("watcher", recorder.CRASH, exc=_boom("first"))
        recorder.flush()
        recorder.record("watcher", recorder.CRASH, exc=_boom("second"))
        recorder.record("watcher", recorder.CRASH, exc=_other_boom())
        recorder.flush()
        by_function = {e["frames"][-1]["function"]: e for e in self.events()}
        self.assertEqual(set(by_function), {"_boom", "_other_boom"})
        self.assertEqual(by_function["_boom"]["count"], 2)
        self.assertEqual(by_function["_boom"]["message"], "second")

    def test_the_signature_ignores_line_numbers(self) -> None:
        frames = [{"file": "services/x.py", "line": 10, "function": "f"}]
        recorder.record("x", recorder.FAILING, error_type="E", message="m", frames=frames)
        recorder.record("x", recorder.FAILING, error_type="E", message="m",
                        frames=[{"file": "services/x.py", "line": 99, "function": "f"}])
        recorder.flush()
        [event] = self.events()
        self.assertEqual(event["count"], 2)

    def test_repeats_are_coalesced_and_flushed(self) -> None:
        with patch.object(recorder.time, "time", return_value=1000.0):
            for _ in range(15):
                recorder.record("watcher", recorder.FAILING, exc=_boom())
        [event] = self.events()
        self.assertEqual(event["count"], 1, "only the first one reached the disk")
        recorder.flush()
        [event] = self.events()
        self.assertEqual(event["count"], 15)
        self.assertEqual(len(event["occurrences"]), recorder.MAX_OCCURRENCES)

    def test_a_later_repeat_is_written_after_the_interval(self) -> None:
        clock = [1000.0]
        with patch.object(recorder.time, "time", side_effect=lambda: clock[0]):
            recorder.record("w", recorder.FAILING, exc=_boom())
            clock[0] += recorder.MIN_WRITE_INTERVAL_S + 1
            recorder.record("w", recorder.FAILING, exc=_boom())
        [event] = self.events()
        self.assertEqual(event["count"], 2)

    def test_a_refusal_is_keyed_by_its_file(self) -> None:
        recorder.record("todos", recorder.REFUSAL, error_type="corrupt_document", message="a",
                        subject=str(self.state.parent / "projects" / "p" / ".xo" / "todos.json"))
        recorder.record("todos", recorder.REFUSAL, error_type="corrupt_document", message="b",
                        subject=str(self.state.parent / "projects" / "q" / ".xo" / "todos.json"))
        subjects = sorted(e["subject"] for e in self.events())
        self.assertEqual(subjects, ["<projects>/p/.xo/todos.json", "<projects>/q/.xo/todos.json"])


class PrivacyTests(RecorderSandbox):
    def test_messages_lose_folders_urls_and_secrets(self) -> None:
        secret = "sk_" + "a" * 40
        text = (f"cannot read {self.state}/inbox/inbox.json via https://api.example.com/v1?x=1 "
                f"with api_key={secret} token {secret}")
        recorder.record("inbox", recorder.REFUSAL, error_type="E", message=text)
        [event] = self.events()
        self.assertIn("<state>/inbox/inbox.json", event["message"])
        for leak in (str(self.state), "api.example.com", secret):
            self.assertNotIn(leak, json.dumps(event))
        self.assertLessEqual(len(event["message"]), recorder.MAX_MESSAGE)

    def test_frames_carry_locations_only(self) -> None:
        password = "hunter2-very-secret"

        def leaks() -> None:
            raise ValueError("bad value")

        try:
            leaks()
        except ValueError as exc:
            recorder.record("x", recorder.CRASH, exc=exc)
        [event] = self.events()
        self.assertNotIn(password, json.dumps(event))
        self.assertEqual(set(event["frames"][-1]), {"file", "line", "function"})

    def test_a_failure_outside_this_checkout_keeps_one_short_frame(self) -> None:
        try:
            json.loads("{")
        except ValueError as exc:
            exc.__traceback__ = exc.__traceback__.tb_next  # drop this test's own frame
            recorder.record("x", recorder.CRASH, exc=exc)
        [event] = self.events()
        self.assertEqual(len(event["frames"]), 1)
        self.assertTrue(event["frames"][0]["file"].startswith("<lib>/"))


class CapsTests(RecorderSandbox):
    def test_the_oldest_record_makes_room(self) -> None:
        with patch.object(recorder, "MAX_EVENTS", 3):
            for index in range(4):
                recorder.record(f"c{index}", recorder.FAILING, error_type="E", message="m")
                path = self.state / "setup/health/events"
                for p in path.glob("*.json"):  # make the earlier ones look older
                    os.utime(p, (p.stat().st_mtime - 10, p.stat().st_mtime - 10))
        self.assertEqual(sorted(e["component"] for e in self.events()), ["c1", "c2", "c3"])

    def test_the_total_size_is_capped(self) -> None:
        with patch.object(recorder, "MAX_TOTAL_BYTES", 1):
            recorder.record("a", recorder.FAILING, error_type="E", message="m")
            recorder.record("b", recorder.FAILING, error_type="E", message="m")
        self.assertEqual([e["component"] for e in self.events()], ["b"])

    def test_records_not_seen_for_thirty_days_are_pruned(self) -> None:
        recorder.record("old", recorder.FAILING, error_type="E", message="m")
        recorder.record("new", recorder.FAILING, error_type="E", message="m")
        old = next(p for p in (self.state / "setup/health/events").glob("*.json")
                   if json.loads(p.read_text())["component"] == "old")
        stamp = time.time() - recorder.MAX_AGE_S - 60
        os.utime(old, (stamp, stamp))
        recorder.prune()
        self.assertEqual([e["component"] for e in self.events()], ["new"])


class NeverRaisesTests(RecorderSandbox):
    def test_an_unwritable_store_costs_the_caller_nothing(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores folder permissions")
        (self.state / "setup").mkdir(parents=True)
        (self.state / "setup").chmod(0o500)
        self.addCleanup((self.state / "setup").chmod, 0o755)
        recorder.record("x", recorder.CRASH, exc=_boom())
        recorder.flush()

    def test_a_bad_kind_or_a_damaged_record_never_raises(self) -> None:
        recorder.record("x", "not-a-kind", message="m")
        recorder.record("x", recorder.FAILING, error_type="E", message="m")
        [path] = (self.state / "setup/health/events").glob("*.json")
        path.write_text("{")
        recorder.flush()
        recorder._reset_for_tests()
        recorder.record("x", recorder.FAILING, error_type="E", message="m")
        [event] = self.events()
        self.assertEqual(event["count"], 1, "a damaged record starts over")


if __name__ == "__main__":
    unittest.main()
