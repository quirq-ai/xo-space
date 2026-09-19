"""Every event a project store emits is written once, and the Space view reads it.

Todos, workitems and claims are written through the HTTP API, so their stores,
not the watcher, emit their events. Each line lands once, in the project's log
(``~/.quirq/projects/<pid>/timeline.jsonl``), stamped with the pid and the
project's folder name; the Space log (``~/.quirq/projects/timeline.jsonl``)
gets no copy. The Space view the Inbox's timeline feeder and
``GET /api/xo-projects/timeline`` read is a merge at read time
(``WorkspaceVisualizerScope.read_timeline`` over ``modules.timeline``), and
answers every line a project log holds.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from modules.timeline import service as timeline_service
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
        timeline_service.reset_for_tests()
        self.addCleanup(timeline_service.reset_for_tests)
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

    def space_view(self, event_type: str) -> list[dict]:
        """What the merged Space read answers for one type."""
        scope = scopes.resolve_scope("xo-workspace-visualizer")
        return [line for line in scope.read_timeline(limit=100) if line["type"] == event_type]

    def assert_written_once(self, event_type: str) -> list[dict]:
        """The project log holds the line, stamped; the Space log holds no
        copy; the merged Space view still answers it. Returns the project
        lines of that type."""
        project = [line for line in self.project_lines() if line["type"] == event_type]
        self.assertTrue(project, f"{event_type} is missing from the project timeline")
        for line in project:
            self.assertEqual((line["project_id"], line["pid"]), (PROJECT, PID))
        self.assertEqual([line for line in self.space_lines() if line["type"] == event_type], [],
                         f"{event_type} was copied into the Space log")
        merged = self.space_view(event_type)
        self.assertEqual(len(merged), len(project), f"{event_type} is missing from the merged Space view")
        for line in merged:
            self.assertEqual((line["project_id"], line["pid"]), (PROJECT, PID))
        return project


class TodoEventTests(_Sandbox):
    def test_adding_and_completing_a_todo_are_written_once_and_read_merged(self) -> None:
        todo = todos_store.create_todo(self.xo / "todos.json", runtime="r", content="write the tests")
        [added] = self.assert_written_once("todo.added")
        self.assertEqual(added["todo"]["id"], todo["id"])
        todos_store.update_todo(self.xo / "todos.json", todo["id"], status="completed")
        self.assert_written_once("todo.completed")


class WorkitemEventTests(_Sandbox):
    def test_creating_a_workitem_is_written_once_and_read_merged(self) -> None:
        workitems_store.create_workitem(self.xo / "workitems.json", runtime="r", title="ship it")
        self.assert_written_once("workitem.created")

    def test_claiming_and_releasing_through_the_project_scope(self) -> None:
        scope = scopes.resolve_scope("xo-projects-visualizer", PROJECT)
        workitem_id = str(uuid.uuid4())
        scope.claim_workitem(workitem_id, session_id="s1", runtime="r")
        self.assert_written_once("workitem.claimed")
        self.assertTrue(scope.release_workitem(workitem_id))
        self.assert_written_once("workitem.released")
        # The project scope reads the same log through the module.
        kinds = [line["type"] for line in scope.read_timeline(limit=10)]
        self.assertEqual(kinds, ["workitem.released", "workitem.claimed"])


class SinkTests(_Sandbox):
    EVENT = TaskCreated(
        ts="2026-09-14T10:00:02.798Z", native_session_id="s1", runtime="r",
        task_id="abcd1234", content="x", description=None, active_form=None,
    )

    def test_a_project_line_is_written_once_whether_or_not_the_project_is_named(self) -> None:
        timeline.apply(self.runtime, [self.EVENT])
        [line] = self.project_lines()
        self.assertEqual(line["pid"], PID)
        self.assertNotIn("project_id", line, "the writer did not know the folder name")
        self.assertEqual(self.space_lines(), [], "the Space log takes no copy")
        # The merged read resolves the folder name from the pid.
        [merged] = self.space_view("todo.added")
        self.assertEqual((merged["project_id"], merged["pid"]), (PROJECT, PID))

        later = TaskCreated(
            ts="2026-09-14T10:00:03.000Z", native_session_id="s1", runtime="r",
            task_id="abcd1235", content="y", description=None, active_form=None,
        )
        timeline.apply(self.runtime, [later], project_id=PROJECT)
        self.assertEqual(len(self.project_lines()), 2)
        self.assertEqual(self.project_lines()[1]["project_id"], PROJECT)
        self.assertEqual(self.space_lines(), [])
        self.assertEqual(len(self.space_view("todo.added")), 2)

    def test_a_line_with_no_pid_lands_in_the_space_log(self) -> None:
        written = timeline_service.emit([{"ts": "2026-09-14T10:00:00Z", "type": "project.created"}])
        self.assertEqual(len(written), 1)
        [line] = self.space_lines()
        self.assertEqual(list(line), ["ts", "type"])
        self.assertEqual(self.project_lines(), [])
        [merged] = self.space_view("project.created")
        self.assertNotIn("pid", merged)


if __name__ == "__main__":
    unittest.main()
