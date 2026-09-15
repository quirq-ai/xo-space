"""Rule 1: records about a project carry its ``pid``.

A folder name is a label that changes on rename; the pid in
``.xo/project.json`` is the key machine-local records join on. Timeline lines
take it from the runtime home they are written to
(``~/.quirq/projects/<pid>/``), and Inbox items take it from the event or from
the project's identity file.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.visualizer.ingest.events import TaskCreated
from services.cowork_agent.visualizer.sinks import timeline
from services.inbox import feeders, store

PID = "7deb4a22-0789-497d-9399-a2272579fa06"
SCHEMAS = Path(__file__).resolve().parents[1] / "services" / "cowork_agent" / "visualizer" / "schema"


def _event() -> TaskCreated:
    return TaskCreated(
        ts="2026-09-14T10:00:02.798Z", native_session_id="s1", runtime="claude_code",
        task_id="abcd1234", content="write tests", description=None, active_form=None,
    )


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        env = patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(self.base / "projects"),
            "QUIRQ_STATE_ROOT": str(self.base / "state"),
            "QUIRQ_COMMAND_LOG": "off",
        })
        env.start()
        self.addCleanup(env.stop)

    def project(self, name: str = "demo", pid: str = PID) -> None:
        xo = self.base / "projects" / name / ".xo"
        xo.mkdir(parents=True)
        (xo / "project.json").write_text(json.dumps({
            "schema": 2, "pid": pid, "name": name,
            "owner_user_id": "local", "created_at": "2026-09-14T10:00:00Z",
        }), encoding="utf-8")


class TimelineTests(_Sandbox):
    def read_lines(self, root: Path) -> list[dict]:
        text = (root / "timeline.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]

    def test_a_line_starts_with_ts_and_type_and_carries_the_pid(self) -> None:
        root = self.base / "state" / "projects" / PID
        root.mkdir(parents=True)
        timeline.apply(root, [_event()])
        [line] = self.read_lines(root)
        self.assertEqual(list(line)[:3], ["ts", "type", "pid"])
        self.assertEqual((line["type"], line["pid"]), ("todo.added", PID))
        self.assertEqual(line["session_id"], "s1")

    def test_a_runtime_home_keyed_by_folder_name_stamps_no_pid(self) -> None:
        root = self.base / "state" / "projects" / "demo"
        root.mkdir(parents=True)
        timeline.apply(root, [_event()])
        [line] = self.read_lines(root)
        self.assertNotIn("pid", line)
        self.assertEqual(list(line)[:2], ["ts", "type"])

    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is not installed")
    def test_a_line_with_a_pid_satisfies_the_timeline_schema(self) -> None:
        import jsonschema

        root = self.base / "state" / "projects" / PID
        root.mkdir(parents=True)
        timeline.apply(root, [_event()])
        schema = json.loads((SCHEMAS / "timeline.schema.json").read_text(encoding="utf-8"))
        for line in self.read_lines(root):
            jsonschema.Draft7Validator(schema).validate(line)


class InboxTests(_Sandbox):
    def test_an_item_for_a_project_carries_its_pid(self) -> None:
        self.project()
        item = store.build_item(title="t", project_id="demo")
        self.assertEqual(item["pid"], PID)
        self.assertEqual(list(item).index("pid"), list(item).index("project_id") + 1)

    def test_an_item_without_a_project_or_identity_has_no_pid(self) -> None:
        self.assertIsNone(store.build_item(title="t")["pid"])
        (self.base / "projects" / "bare").mkdir(parents=True)
        self.assertIsNone(store.build_item(title="t", project_id="bare")["pid"])

    def test_the_timeline_feeder_takes_the_pid_from_the_line(self) -> None:
        item = feeders._timeline_item({
            "ts": "2026-09-14T10:00:02.798Z", "type": "session.started", "pid": PID,
            "session_id": "s1", "runtime": "claude_code", "project_id": "demo",
        })
        self.assertEqual((item["project_id"], item["pid"]), ("demo", PID))

    def test_a_hand_edited_invalid_pid_reads_as_none(self) -> None:
        raw = {"id": "deadbeef", "ts": "2026-09-14T10:00:00Z", "title": "t", "pid": "../escape"}
        self.assertIsNone(store._normalize_item(raw, "2026-09-14T10:00:00Z")["pid"])


if __name__ == "__main__":
    unittest.main()
