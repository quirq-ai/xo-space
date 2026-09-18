"""The four record rules, checked on what the writers actually write.

``tests/test_quirq_state_layout.py`` holds the *sample* state root to the rules
(``tests/fixtures/quirq-state/README.md``). This file feeds the real writers the
forms agents really produce, so an agent that writes time its own way still
lands on disk the same as every other: hermes and antigravity stamp events with
``isoformat()`` (``+00:00``, microseconds), claude_code with milliseconds and
``Z``.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent import project_layout
from services.cowork_agent.visualizer.ingest.events import MessageObserved, SessionFirstSeen
from services.cowork_agent.visualizer.sinks import stats, timeline
from services.timestamps import canonical_ts

PROJECT = "demo"
PID = "7deb4a22-0789-497d-9399-a2272579fa06"

# How each agent writes the time of the same moment.
AGENT_TIMES = {
    "hermes": "2026-09-15T19:33:17.796439+00:00",
    "antigravity": "2026-09-15T19:33:17.796000+00:00",
    "claude_code": "2026-09-15T19:33:17.796Z",
}


class CanonicalTsTests(unittest.TestCase):
    def test_every_agent_form_becomes_utc_ending_in_z(self) -> None:
        for agent, value in AGENT_TIMES.items():
            with self.subTest(agent=agent):
                self.assertEqual(canonical_ts(value), "2026-09-15T19:33:17.796Z")

    def test_whole_seconds_stay_whole_seconds(self) -> None:
        self.assertEqual(canonical_ts("2026-09-14T13:52:49Z"), "2026-09-14T13:52:49Z")
        self.assertEqual(canonical_ts("2026-09-14T13:52:49+00:00"), "2026-09-14T13:52:49Z")

    def test_an_offset_is_converted_to_utc(self) -> None:
        self.assertEqual(canonical_ts("2026-09-15T21:26:23+05:30"), "2026-09-15T15:56:23Z")

    def test_a_value_that_does_not_parse_is_kept_not_guessed(self) -> None:
        for value in ("", "not a time", None, 1789500801965):
            with self.subTest(value=value):
                self.assertEqual(canonical_ts(value), value)


class WatcherSinkTimeTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        env = patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(base / "projects"),
            "QUIRQ_STATE_ROOT": str(base / "state"),
            "QUIRQ_COMMAND_LOG": "off",
        })
        env.start()
        self.addCleanup(env.stop)
        xo = base / "projects" / PROJECT / ".xo"
        xo.mkdir(parents=True)
        (xo / "project.json").write_text(json.dumps({
            "schema": 2, "pid": PID, "name": PROJECT,
            "owner_user_id": "local", "created_at": "2026-09-14T10:00:00Z",
        }), encoding="utf-8")
        self.runtime = project_layout.runtime_dir_for_project(PROJECT, create=True)

    def _events(self) -> list[SessionFirstSeen]:
        return [
            SessionFirstSeen(ts=value, native_session_id=f"s-{agent}", runtime=agent, cwd="")
            for agent, value in AGENT_TIMES.items()
        ]

    @staticmethod
    def _lines(path: Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_project_and_space_timeline_lines_end_in_z(self) -> None:
        timeline.apply(self.runtime, self._events(), project_id=PROJECT)
        for path in (self.runtime / "timeline.jsonl", project_layout.workspace_timeline_path()):
            lines = self._lines(path)
            self.assertEqual(len(lines), len(AGENT_TIMES), path)
            for line in lines:
                with self.subTest(path=path.name, runtime=line["runtime"]):
                    self.assertEqual(list(line)[:2], ["ts", "type"])
                    self.assertEqual(line["ts"], "2026-09-15T19:33:17.796Z")

    def test_stats_session_times_end_in_z(self) -> None:
        messages = [
            MessageObserved(ts=value, native_session_id=f"s-{agent}", runtime=agent, role="user")
            for agent, value in AGENT_TIMES.items()
        ]
        self.assertTrue(stats.apply(self.runtime, self._events() + messages))
        doc = json.loads((self.runtime / "stats.json").read_text(encoding="utf-8"))
        totals = doc["_session_totals"]
        self.assertEqual(set(totals), {f"s-{agent}" for agent in AGENT_TIMES})
        for sid, row in totals.items():
            with self.subTest(session=sid):
                self.assertEqual(row["first_ts"], "2026-09-15T19:33:17.796Z")
                self.assertEqual(row["last_ts"], "2026-09-15T19:33:17.796Z")


if __name__ == "__main__":
    unittest.main()
