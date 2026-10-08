"""services.health.session: a run that didn't shut down is noticed at the next start."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from services.health import annotations, recorder, session

REPO = Path(__file__).resolve().parents[1]


class SessionSandbox(unittest.TestCase):
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
        # pytest owns faulthandler in this process; the real hand-off is
        # exercised in a child process below.
        for name in ("_enable_faulthandler", "_disable_faulthandler"):
            p = patch.object(session, name)
            p.start()
            self.addCleanup(p.stop)

    @property
    def health(self) -> Path:
        return self.state / "setup" / "health"

    def events(self, kind: str) -> list[dict]:
        folder = self.health / "events"
        found = [json.loads(p.read_text()) for p in folder.glob("*.json")] if folder.exists() else []
        return [e for e in found if e["kind"] == kind]

    def write_session(self, **fields) -> None:
        self.health.mkdir(parents=True, exist_ok=True)
        document = {"schema": 1, "boot_id": "previous", "pid": 999999, "started_at": "2026-01-01T09:00:00Z",
                    "alive_at": "2026-01-01T09:20:00Z", "version": "v1.0.0", "clean_exit_at": None, **fields}
        (self.health / "session.json").write_text(json.dumps(document))


class BeginTests(SessionSandbox):
    def test_a_first_start_writes_its_marker_and_annotations(self) -> None:
        boot_id = session.begin()
        marker = json.loads((self.health / "session.json").read_text())
        self.assertEqual((marker["boot_id"], marker["pid"], marker["clean_exit_at"]), (boot_id, os.getpid(), None))
        notes = json.loads((self.health / "boots" / f"{boot_id}.json").read_text())
        self.assertEqual(notes["boot_id"], boot_id)
        self.assertIn("watcher", notes["switches"])
        self.assertEqual(self.events("unclean_exit"), [])

    def test_a_run_that_never_shut_down_is_recorded_at_the_next_start(self) -> None:
        self.write_session()
        heartbeat = self.state / "cache" / "heartbeat.json"
        heartbeat.parent.mkdir(parents=True)
        heartbeat.write_text(json.dumps({"schema": 1, "last_tick_at": "2026-01-01T09:29:59Z"}))
        session.begin()
        [event] = self.events("unclean_exit")
        self.assertEqual(event["details"]["previous_boot_id"], "previous")
        self.assertEqual(event["details"]["ended_around"], "2026-01-01T09:29:59Z")
        self.assertEqual(event["details"]["previous_version"], "v1.0.0")

    def test_a_clean_shutdown_is_not_recorded(self) -> None:
        self.write_session(clean_exit_at="2026-01-01T09:30:00Z")
        session.begin()
        self.assertEqual(self.events("unclean_exit"), [])

    def test_another_server_still_running_on_this_state_folder_is_not_an_unclean_exit(self) -> None:
        self.write_session()
        with patch.object(session, "_still_running", return_value=True):
            session.begin()
        self.assertEqual(self.events("unclean_exit"), [])

    def test_two_unclean_exits_are_one_record_with_a_count(self) -> None:
        self.write_session()
        session.begin()
        recorder.flush()
        self.write_session()
        recorder._last_write.clear()
        session.begin()
        [event] = self.events("unclean_exit")
        self.assertEqual(event["count"], 2)


class EndTests(SessionSandbox):
    def test_a_clean_end_is_marked_and_repeats_are_written(self) -> None:
        session.begin()
        with patch.object(recorder.time, "time", return_value=5000.0):
            for _ in range(3):
                recorder.record("w", recorder.FAILING, error_type="E", message="m")
        session.end()
        marker = json.loads((self.health / "session.json").read_text())
        self.assertTrue(marker["clean_exit_at"])
        [event] = self.events("failing")
        self.assertEqual(event["count"], 3)

    def test_alive_is_refreshed_at_most_once_a_minute(self) -> None:
        session.begin(now=1000.0)
        session.mark_alive(now=1030.0)
        self.assertEqual(json.loads((self.health / "session.json").read_text())["alive_at"], session._stamp(1000.0))
        session.mark_alive(now=1061.0)
        self.assertEqual(json.loads((self.health / "session.json").read_text())["alive_at"], session._stamp(1061.0))


class FatalTests(SessionSandbox):
    def test_a_faulthandler_dump_becomes_a_fatal_record(self) -> None:
        self.health.mkdir(parents=True)
        (self.health / "fatal.log").write_text(
            "Fatal Python error: Segmentation fault\n\n"
            "Thread 0x00007e (most recent call first):\n"
            '  File "/usr/lib/python3.14/threading.py", line 369 in wait\n'
            '  File "/usr/lib/python3.14/queue.py", line 199 in get\n\n'
            "Current thread 0x00007f (most recent call first):\n"
            f'  File "{REPO}/services/cowork_agent/visualizer/watcher.py", line 212 in _tick_body\n'
            f'  File "{REPO}/server.py", line 800 in lifespan\n'
            '  File "/usr/lib/python3.14/asyncio/base_events.py", line 1 in run_forever\n\n'
            "Thread 0x00007d (most recent call first):\n"
            '  File "/usr/lib/python3.14/selectors.py", line 452 in select\n')
        session.begin()
        [event] = self.events("fatal")
        self.assertEqual(event["message"], "Fatal Python error: Segmentation fault")
        self.assertEqual(event["frames"][-1], {"file": "services/cowork_agent/visualizer/watcher.py", "line": 212,
                                               "function": "_tick_body"})
        # Only the crashed thread's frames: idle threads say nothing about where.
        self.assertNotIn("<lib>/queue.py", json.dumps(event["frames"]))
        self.assertNotIn("<lib>/selectors.py", json.dumps(event["frames"]))
        self.assertFalse((self.health / "fatal.log").exists())
        self.assertTrue((self.health / "fatal.log.1").exists(), "read once, then kept beside")

    def test_a_real_segfault_in_a_child_process_is_recorded_at_the_next_start(self) -> None:
        # The real hand-off: the child points faulthandler at fatal.log via
        # begin(), then crashes the way a broken C extension would.
        child = ("from services.health import session; session.begin(); "
                 "import faulthandler; faulthandler._sigsegv()")
        env = {**os.environ, "QUIRQ_STATE_ROOT": str(self.state)}
        result = subprocess.run([sys.executable, "-c", child], cwd=REPO, env=env, capture_output=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        recorder._reset_for_tests()
        session.begin()
        self.assertEqual(len(self.events("unclean_exit")), 1)
        [fatal] = self.events("fatal")
        self.assertIn("Segmentation fault", fatal["message"])


class AnnotationTests(SessionSandbox):
    def test_annotations_never_carry_keys_or_the_space_id(self) -> None:
        secrets = {"XO_API_KEY": "xo-secret-value-123", "XO_SPACE_ID": "space-secret-456",
                   "ANTHROPIC_API_KEY": "sk-ant-secret-789", "AGENT_NAME": "sample_agent"}
        with patch.dict(os.environ, secrets):
            notes = annotations.collect("boot", "2026-01-01T00:00:00Z")
        dumped = json.dumps(notes)
        for name in ("XO_API_KEY", "XO_SPACE_ID", "ANTHROPIC_API_KEY"):
            self.assertNotIn(secrets[name], dumped)
        self.assertEqual(notes["agent"], "sample_agent")
        self.assertIsInstance(notes["version"], str)


if __name__ == "__main__":
    unittest.main()


class MarkerOwnershipTests(SessionSandbox):
    """Review finding 4 and the shutdown race."""

    def test_a_run_never_changes_another_runs_marker(self) -> None:
        session.begin()
        self.write_session(boot_id="another-server", clean_exit_at=None)
        session.end()
        marker = json.loads((self.health / "session.json").read_text())
        self.assertEqual((marker["boot_id"], marker["clean_exit_at"]), ("another-server", None))

    def test_a_refresh_after_the_end_cannot_undo_the_clean_mark(self) -> None:
        session.begin(now=1000.0)
        session.end(now=1100.0)
        session.mark_alive(now=5000.0)
        marker = json.loads((self.health / "session.json").read_text())
        self.assertEqual(marker["clean_exit_at"], session._stamp(1100.0))
        self.assertEqual(marker["alive_at"], session._stamp(1100.0))

    def test_annotation_roots_carry_no_user_name(self) -> None:
        with patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(Path.home() / ".quirq-test"),
                                     "XO_PROJECTS_ROOT": "~/xo-projects", "QUIRQ_HOST_PROJECTS_ROOT": "/home/alice/xo"}):
            notes = annotations.collect("boot", "2026-01-01T00:00:00Z")
        self.assertEqual(notes["roots"], {"state": "~/.quirq-test", "projects": "~/xo"})
