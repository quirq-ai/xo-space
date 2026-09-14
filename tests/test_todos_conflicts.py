"""A ``todos.json`` the store cannot read is refused, never overwritten.

``.xo/`` travels through git, so a merge can leave conflict markers in
``todos.json``. Agents resolve those conflicts, which only works while the file
still holds both sides: a todo write must refuse it (409 through the API) and
leave its bytes exactly as they were.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent.bff import visualizer as visualizer_routes
from services.cowork_agent.visualizer import todos_store

PROJECT = "demo"

CONFLICTED = b"""{
  "$schema": "xo/todos.schema.json",
  "schema": 2,
<<<<<<< HEAD
  "updated_at": "2026-09-14T10:00:00Z",
  "sessions": {"_project": {"runtime": "a", "todos": [{"id": "aaaa0001", "content": "mine"}]}}
=======
  "updated_at": "2026-09-14T11:00:00Z",
  "sessions": {"_project": {"runtime": "b", "todos": [{"id": "bbbb0002", "content": "theirs"}]}}
>>>>>>> origin/main
}
"""


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
        self.todos = self.xo / "todos.json"

    def assert_refused(self, call, code: str = "corrupt_document") -> None:
        before = self.todos.read_bytes()
        with self.assertRaises(todos_store.TodosStoreError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.todos.read_bytes(), before)


class StoreTests(_Sandbox):
    def test_every_write_refuses_a_conflicted_file_and_leaves_its_bytes(self) -> None:
        self.todos.write_bytes(CONFLICTED)
        self.assert_refused(lambda: todos_store.create_todo(self.todos, runtime="r", content="new"))
        self.assert_refused(lambda: todos_store.update_todo(self.todos, "aaaa0001", status="completed"))
        self.assert_refused(lambda: todos_store.delete_todo(self.todos, "bbbb0002"))

    def test_an_empty_file_and_a_non_object_sessions_map_are_refused(self) -> None:
        self.todos.write_bytes(b"")
        self.assert_refused(lambda: todos_store.create_todo(self.todos, runtime="r", content="new"))
        self.todos.write_text(json.dumps({"schema": 2, "sessions": []}), encoding="utf-8")
        self.assert_refused(lambda: todos_store.create_todo(self.todos, runtime="r", content="new"))

    def test_a_newer_schema_is_refused(self) -> None:
        self.todos.write_text(json.dumps({"schema": 3, "sessions": {}}), encoding="utf-8")
        self.assert_refused(
            lambda: todos_store.create_todo(self.todos, runtime="r", content="new"),
            code="unsupported_schema",
        )

    def test_an_older_schema_is_still_upgraded_on_write(self) -> None:
        old = {"schema": 1, "sessions": {"_project": {"runtime": "r", "todos": [
            {"id": "cccc0003", "content": "kept", "status": "pending"},
        ]}}}
        self.todos.write_text(json.dumps(old), encoding="utf-8")
        todos_store.create_todo(self.todos, runtime="r", content="new")
        written = json.loads(self.todos.read_text(encoding="utf-8"))
        self.assertEqual(written["schema"], todos_store.TODOS_SCHEMA)
        contents = [t["content"] for t in written["sessions"]["_project"]["todos"]]
        self.assertEqual(contents, ["kept", "new"])

    def test_a_missing_file_is_still_created(self) -> None:
        todos_store.create_todo(self.todos, runtime="r", content="first")
        self.assertEqual(json.loads(self.todos.read_text(encoding="utf-8"))["schema"], 2)

    def test_reading_one_todo_stays_lenient(self) -> None:
        self.todos.write_bytes(CONFLICTED)
        self.assertIsNone(todos_store.get_todo(self.todos, "aaaa0001"))


class RouteTests(_Sandbox):
    def client(self) -> TestClient:
        app = FastAPI()
        app.include_router(visualizer_routes.router)
        return TestClient(app)

    def test_writes_answer_409_and_leave_the_file_alone(self) -> None:
        self.todos.write_bytes(CONFLICTED)
        base = f"/api/xo-projects/{PROJECT}/todos"
        c = self.client()
        answers = [
            c.post(base, json={"runtime": "r", "content": "new"}),
            c.patch(f"{base}/aaaa0001", json={"status": "completed"}),
            c.delete(f"{base}/bbbb0002"),
        ]
        for answer in answers:
            with self.subTest(method=answer.request.method):
                self.assertEqual(answer.status_code, 409, answer.text)
                detail = answer.json()["detail"]
                self.assertEqual(detail["code"], "corrupt_document")
                self.assertNotIn(str(self.todos), detail["message"])
        self.assertEqual(self.todos.read_bytes(), CONFLICTED)


if __name__ == "__main__":
    unittest.main()
