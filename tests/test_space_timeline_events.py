"""The Space timeline carries every event a project timeline does.

Todos, workitems and claims are written through the HTTP API, so their stores,
not the watcher, emit their events. Each line lands in the project's timeline
and, tagged with the project's folder name, in the Space timeline
(``~/.quirq/projects/timeline.jsonl``), which the Inbox's timeline feeder and
``GET /api/xo-projects/timeline`` read.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent import project_layout, scopes
from services.cowork_agent.visualizer import todos_store, workitems_store
from services.cowork_agent.visualizer.ingest.events import TaskCreated
from services.cowork_agent.visualizer.sinks import timeline

PROJECT = "demo"
PID = "7deb4a22-0789-497d-9399-a2272579fa06"


class _Sandbox(unittest.TestCase):
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
        self.xo = base / "projects" / PROJECT / ".xo"
        self.xo.mkdir(parents=True)
        (self.xo / "project.json").write_text(json.dumps({
            "schema": 2, "pid": PID, "name": PROJECT,
            "owner_user_id": "local", "created_at": "2026-09-14T10:00:00Z",
        }), encoding="utf-8")
        self.runtime = project_layout.runtime_dir_for_project(PROJECT, create=True)

    @staticmethod
    def _read(path: Path) -> list[dict]:
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def project_lines(self) -> list[dict]:
        return self._read(self.runtime / "timeline.jsonl")

    def space_lines(self) -> list[dict]:
        return self._read(project_layout.workspace_timeline_path())

    def assert_in_both(self, event_type: str) -> list[dict]:
        project = [line for line in self.project_lines() if line["type"] == event_type]
        space = [line for line in self.space_lines() if line["type"] == event_type]
        self.assertTrue(project, f"{event_type} is missing from the project timeline")
        self.assertEqual(len(space), len(project), f"{event_type} is missing from the Space timeline")
        for line in space:
            self.assertEqual((line["project_id"], line["pid"]), (PROJECT, PID))
        return space


class TodoEventTests(_Sandbox):
    def test_adding_and_completing_a_todo_reach_the_space_timeline(self) -> None:
        todo = todos_store.create_todo(self.xo / "todos.json", runtime="r", content="write the tests")
        [added] = self.assert_in_both("todo.added")
        self.assertEqual(added["todo"]["id"], todo["id"])
        todos_store.update_todo(self.xo / "todos.json", todo["id"], status="completed")
        self.assert_in_both("todo.completed")


class WorkitemEventTests(_Sandbox):
    def test_creating_a_workitem_reaches_the_space_timeline(self) -> None:
        workitems_store.create_workitem(self.xo / "workitems.json", runtime="r", title="ship it")
        self.assert_in_both("workitem.created")

    def test_claiming_and_releasing_through_the_project_scope(self) -> None:
        scope = scopes.resolve_scope("xo-projects-visualizer", PROJECT)
        workitem_id = str(uuid.uuid4())
        scope.claim_workitem(workitem_id, session_id="s1", runtime="r")
        self.assert_in_both("workitem.claimed")
        self.assertTrue(scope.release_workitem(workitem_id))
        self.assert_in_both("workitem.released")


class SinkTests(_Sandbox):
    def test_the_space_timeline_is_written_only_when_the_project_is_named(self) -> None:
        event = TaskCreated(
            ts="2026-09-14T10:00:02.798Z", native_session_id="s1", runtime="r",
            task_id="abcd1234", content="x", description=None, active_form=None,
        )
        timeline.apply(self.runtime, [event])
        self.assertEqual(len(self.project_lines()), 1)
        self.assertEqual(self.space_lines(), [])
        timeline.apply(self.runtime, [event], project_id=PROJECT)
        self.assertEqual(len(self.space_lines()), 1)


if __name__ == "__main__":
    unittest.main()
