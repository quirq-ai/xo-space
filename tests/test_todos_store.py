"""Todo read/write behaviour — the API as the todo event source.

Before this module nothing in the repo exercised a todo write. The
three things it holds the implementation to (syncplan §7, T7–T9):

* **parity** — ``todos.json`` is byte-identical under every
  ``AGENT_NAME`` for the same sequence of calls, and a todo created
  through the API produces a ``todo.added`` timeline line and bumps
  ``taskCount`` whatever backend is active;
* **one source** — the watcher no longer feeds task events to any sink,
  so a transcript replay cannot write, or resurrect, a todo;
* **soft delete** — a deleted todo is tombstoned rather than removed,
  is hidden from reads, cannot be edited back to life, and survives a
  replay.

Never touches the real ``~/xo-projects`` or ``~/.quirq``: both roots are
redirected into a temp dir (each helper re-reads the env per call), and
the todos file is addressed by path.
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

from routers.cowork_agent.bff._visualizer_models import Todo
from routers.cowork_agent.bff.visualizer import (
    _make_todo_model,
    _shape_todos,
    router as visualizer_router,
)
from services.cowork_agent.visualizer import todos_store, watcher
from services.cowork_agent.visualizer.ingest.events import (
    FileTouched,
    MessageObserved,
    TaskCreated,
    TaskStatusChanged,
)
from services.cowork_agent import project_layout
from services.cowork_agent.visualizer.sinks import sessions_augment, timeline


AGENTS = ("claude_code", "openclaw", "hermes", "codex", "antigravity")


class _StoreCase(unittest.TestCase):
    """Temp project + redirected roots, shared by every case below."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.xo_dir = tmp / "xo-projects" / "demo" / ".xo"
        self.xo_dir.mkdir(parents=True)
        self.todos_path = self.xo_dir / "todos.json"
        env = patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(tmp / "xo-projects"),
            "QUIRQ_STATE_ROOT": str(tmp / "quirq"),
        })
        env.start()
        self.addCleanup(env.stop)

    # ── helpers ──────────────────────────────────────────────────────
    def runtime_dir(self) -> Path:
        """Where the derived views live since syncplan T19.

        ``todos.json`` stays in the synced ``.xo/``; the timeline and the
        counters it drives moved to ``~/.quirq/projects/<key>/``. Resolved
        through the chokepoint so the test asserts against wherever the code
        genuinely writes, not against a second hand-built copy of the layout.
        """
        path = project_layout.runtime_dir_for_project("demo")
        assert path is not None, "demo project has no runtime home"
        return path

    def document(self) -> dict:
        return json.loads(self.todos_path.read_text(encoding="utf-8"))

    def stored_todos(self, session: str = "_project") -> list[dict]:
        return self.document()["sessions"][session]["todos"]

    def timeline_lines(self) -> list[dict]:
        path = self.runtime_dir() / "timeline.jsonl"
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def task_count(self, session: str = "_project") -> dict:
        raw = json.loads(
            (self.runtime_dir() / "sessions" / "sessions-augment.json").read_text("utf-8")
        )
        return raw["sessions"][session]["taskCount"]


class CreateTests(_StoreCase):
    def test_create_writes_a_schema_2_record_with_timestamps(self) -> None:
        todo = todos_store.create_todo(
            self.todos_path, runtime="codex", content="ship it",
            description="d", active_form="shipping it",
        )
        doc = self.document()
        self.assertEqual(doc["schema"], todos_store.TODOS_SCHEMA)
        self.assertEqual(doc["schema"], 2)
        self.assertEqual(len(todo["id"]), 8)

        on_disk = self.stored_todos()
        self.assertEqual(len(on_disk), 1)
        self.assertEqual(on_disk[0], todo)
        self.assertEqual(todo["status"], "pending")
        self.assertEqual(todo["created_at"], todo["updated_at"])
        self.assertIsNone(todo["deleted_at"])
        self.assertIsNone(todo["deleted_by"])

    def test_the_created_dict_is_a_legal_wire_model(self) -> None:
        """``Todo`` forbids extra keys, so every field the store writes
        has to be declared or the response 500s."""
        todo = todos_store.create_todo(
            self.todos_path, runtime="codex", content="c",
        )
        model = _make_todo_model(todo)
        self.assertIsInstance(model, Todo)
        self.assertEqual(model.id, todo["id"])
        self.assertIsNone(model.deleted_at)
        # And the raw dict itself, key for key.
        self.assertEqual(Todo(**todo).id, todo["id"])

    def test_a_corrupt_document_is_repaired_rather_than_inherited(self) -> None:
        """One owner, nothing foreign to preserve: the write repairs the
        file instead of refusing (syncplan §3 corrupt-document rules)."""
        for bad in ('{"sessions": ', "[1, 2, 3]", ""):
            with self.subTest(content=bad):
                self.setUp()  # a fresh project per case
                self.todos_path.write_text(bad, encoding="utf-8")
                todo = todos_store.create_todo(
                    self.todos_path, runtime="codex", content="c",
                )
                self.assertEqual(
                    [t["id"] for t in self.stored_todos()], [todo["id"]]
                )
                self.assertEqual(self.document()["schema"], 2)

    def test_ids_are_unique_across_sessions(self) -> None:
        first = todos_store.create_todo(
            self.todos_path, runtime="codex", content="a", session_id="s1",
        )
        second = todos_store.create_todo(
            self.todos_path, runtime="codex", content="b", session_id="s2",
        )
        self.assertNotEqual(first["id"], second["id"])
        found = todos_store.get_todo(self.todos_path, second["id"])
        self.assertIsNotNone(found)
        self.assertEqual(found[0], "s2")


class EventSourceTests(_StoreCase):
    """T7 — the timeline and the counters come from the API path."""

    def test_create_emits_todo_added_and_increments_task_count(self) -> None:
        todo = todos_store.create_todo(
            self.todos_path, runtime="codex", content="ship it",
        )
        lines = self.timeline_lines()
        self.assertEqual([ln["type"] for ln in lines], ["todo.added"])
        self.assertEqual(lines[0]["todo"]["id"], todo["id"])
        self.assertEqual(lines[0]["session_id"], todos_store.PROJECT_SESSION)
        self.assertEqual(lines[0]["runtime"], "codex")

        counts = self.task_count()
        self.assertEqual(counts["total"], 1)
        self.assertEqual(counts["pending"], 1)

    def test_the_same_call_behaves_the_same_under_every_backend(self) -> None:
        """The acceptance criterion, stated as a test: nothing on this
        path may read the active agent."""
        for agent in AGENTS:
            with self.subTest(agent=agent), patch.dict(
                os.environ, {"AGENT_NAME": agent}
            ):
                self.setUp()  # a fresh project per backend
                todos_store.create_todo(
                    self.todos_path, runtime=agent, content="c",
                )
                self.assertEqual(
                    [ln["type"] for ln in self.timeline_lines()], ["todo.added"]
                )
                self.assertEqual(self.task_count()["total"], 1)

    def test_every_status_transition_reaches_the_timeline(self) -> None:
        todo = todos_store.create_todo(
            self.todos_path, runtime="codex", content="c",
        )
        for status in ("in_progress", "completed", "cancelled"):
            todos_store.update_todo(self.todos_path, todo["id"], status=status)

        lines = self.timeline_lines()
        self.assertEqual(
            [ln["type"] for ln in lines],
            ["todo.added", "todo.status_changed", "todo.completed",
             "todo.status_changed"],
        )
        self.assertEqual(lines[1]["status"], "in_progress")
        self.assertEqual(lines[1]["todo_id"], todo["id"])
        self.assertEqual(lines[3]["status"], "cancelled")

    def test_a_non_default_initial_status_lands_in_the_right_bucket(self) -> None:
        todos_store.create_todo(
            self.todos_path, runtime="codex", content="c", status="in_progress",
        )
        counts = self.task_count()
        self.assertEqual(counts["total"], 1)
        self.assertEqual(counts["pending"], 0)
        self.assertEqual(counts["in_progress"], 1)

    def test_a_status_change_moves_the_counter(self) -> None:
        todo = todos_store.create_todo(
            self.todos_path, runtime="codex", content="c",
        )
        todos_store.update_todo(self.todos_path, todo["id"], status="completed")
        counts = self.task_count()
        self.assertEqual(counts["total"], 1)
        self.assertEqual(counts["pending"], 0)
        self.assertEqual(counts["completed"], 1)

    def test_an_update_that_changes_nothing_writes_and_emits_nothing(self) -> None:
        todo = todos_store.create_todo(
            self.todos_path, runtime="codex", content="c",
        )
        before = self.todos_path.read_bytes()
        lines_before = len(self.timeline_lines())

        same = todos_store.update_todo(
            self.todos_path, todo["id"], status="pending", content="c",
        )
        self.assertEqual(same["updated_at"], todo["updated_at"])
        self.assertEqual(self.todos_path.read_bytes(), before)
        self.assertEqual(len(self.timeline_lines()), lines_before)

    def test_a_content_edit_stamps_updated_at_without_a_timeline_line(self) -> None:
        todo = todos_store.create_todo(
            self.todos_path, runtime="codex", content="c",
        )
        with patch.object(todos_store, "_now_iso", return_value="2030-01-01T00:00:00Z"):
            edited = todos_store.update_todo(
                self.todos_path, todo["id"], content="c2",
            )
        self.assertEqual(edited["content"], "c2")
        self.assertEqual(edited["updated_at"], "2030-01-01T00:00:00Z")
        self.assertEqual([ln["type"] for ln in self.timeline_lines()], ["todo.added"])


class ParityTests(_StoreCase):
    """T8 — byte-identical output for the same sequence of calls."""

    def _run_sequence(self) -> bytes:
        stamps = iter([f"2030-01-01T00:00:{n:02d}Z" for n in range(30)])
        ids = iter([f"id{n:06d}" for n in range(30)])

        class _Uuid:
            def __init__(self, value: str) -> None:
                self.hex = value + "0" * (32 - len(value))

        with patch.object(todos_store, "_now_iso", side_effect=lambda: next(stamps)), \
             patch.object(todos_store.uuid, "uuid4", side_effect=lambda: _Uuid(next(ids))):
            first = todos_store.create_todo(
                self.todos_path, runtime="r", content="one",
            )
            second = todos_store.create_todo(
                self.todos_path, runtime="r", content="two",
                session_id="s1", status="in_progress",
            )
            todos_store.update_todo(self.todos_path, first["id"], status="completed")
            todos_store.delete_todo(self.todos_path, second["id"], deleted_by="u1")
        return self.todos_path.read_bytes()

    def test_todos_json_is_identical_under_every_agent_name(self) -> None:
        rendered = {}
        for agent in AGENTS:
            self.setUp()  # fresh project per backend
            with patch.dict(os.environ, {"AGENT_NAME": agent}):
                rendered[agent] = self._run_sequence()
        distinct = set(rendered.values())
        self.assertEqual(
            len(distinct), 1,
            "todos.json differs by backend: "
            + ", ".join(sorted(rendered)),
        )
        # And the fixed sequence really did produce a full document.
        doc = json.loads(next(iter(distinct)))
        self.assertEqual(doc["schema"], 2)
        self.assertEqual(sorted(doc["sessions"]), ["_project", "s1"])

    def test_the_watcher_has_no_todo_sink_left(self) -> None:
        with self.assertRaises(ImportError):
            __import__("services.cowork_agent.visualizer.sinks.todos")
        self.assertFalse(hasattr(watcher, "todos"))


class ReplayTests(_StoreCase):
    """T8 — transcript task events are not a second writer."""

    def test_sink_events_drops_the_task_family_only(self) -> None:
        kept = [
            MessageObserved(ts="t", native_session_id="s", runtime="r", role="user"),
            FileTouched(ts="t", native_session_id="s", runtime="r",
                        relative_path="a.py", created=True),
        ]
        dropped = [
            TaskCreated(ts="t", native_session_id="s", runtime="r",
                        task_id="1", content="native"),
            TaskStatusChanged(ts="t", native_session_id="s", runtime="r",
                              task_id="1", status="completed"),
        ]
        self.assertEqual(watcher._sink_events(kept + dropped), kept)

    def test_a_replay_cannot_resurrect_a_deleted_todo(self) -> None:
        todo = todos_store.create_todo(
            self.todos_path, runtime="codex", content="c",
        )
        todos_store.delete_todo(self.todos_path, todo["id"])
        after_delete = self.todos_path.read_bytes()

        # An offset replay: the source re-emits the whole task family for
        # this session. Everything the watcher would do with it.
        replay = [
            TaskCreated(ts="2030-01-01T00:00:00Z", native_session_id="_project",
                        runtime="codex", task_id=todo["id"], content="c"),
            TaskStatusChanged(ts="2030-01-01T00:00:01Z",
                              native_session_id="_project", runtime="codex",
                              task_id=todo["id"], status="completed"),
        ]
        for_sinks = watcher._sink_events(replay)
        sessions_augment.apply(self.xo_dir, for_sinks)
        timeline.apply(self.xo_dir, for_sinks)

        self.assertEqual(self.todos_path.read_bytes(), after_delete)
        self.assertTrue(todos_store.is_deleted(self.stored_todos()[0]))
        self.assertIsNone(todos_store.get_todo(self.todos_path, todo["id"]))


class SoftDeleteTests(_StoreCase):
    """T9 — the tombstone."""

    def test_delete_tombstones_rather_than_removes(self) -> None:
        todo = todos_store.create_todo(
            self.todos_path, runtime="codex", content="c", status="cancelled",
        )
        self.assertTrue(
            todos_store.delete_todo(self.todos_path, todo["id"], deleted_by="u1")
        )

        on_disk = self.stored_todos()
        self.assertEqual(len(on_disk), 1, "the record was removed, not tombstoned")
        row = on_disk[0]
        self.assertIsNotNone(row["deleted_at"])
        self.assertEqual(row["deleted_by"], "u1")
        # status carries the decision, not the deletion.
        self.assertEqual(row["status"], "cancelled")

    def test_delete_is_idempotent_and_never_404s(self) -> None:
        todo = todos_store.create_todo(
            self.todos_path, runtime="codex", content="c",
        )
        self.assertTrue(todos_store.delete_todo(self.todos_path, todo["id"]))
        self.assertFalse(todos_store.delete_todo(self.todos_path, todo["id"]))
        self.assertFalse(todos_store.delete_todo(self.todos_path, "nosuchid"))

    def test_a_deleted_todo_is_not_readable_unless_asked_for(self) -> None:
        todo = todos_store.create_todo(
            self.todos_path, runtime="codex", content="c",
        )
        todos_store.delete_todo(self.todos_path, todo["id"])
        self.assertIsNone(todos_store.get_todo(self.todos_path, todo["id"]))
        found = todos_store.get_todo(
            self.todos_path, todo["id"], include_deleted=True
        )
        self.assertIsNotNone(found)
        self.assertEqual(found[1]["id"], todo["id"])

    def test_a_deleted_todo_cannot_be_edited_back_to_life(self) -> None:
        todo = todos_store.create_todo(
            self.todos_path, runtime="codex", content="c",
        )
        todos_store.delete_todo(self.todos_path, todo["id"])
        with self.assertRaises(todos_store.TodosStoreError) as raised:
            todos_store.update_todo(
                self.todos_path, todo["id"], status="in_progress"
            )
        self.assertEqual(raised.exception.code, "todo_not_found")
        self.assertEqual(self.stored_todos()[0]["status"], "pending")

    def test_delete_stops_counting_the_task(self) -> None:
        todo = todos_store.create_todo(
            self.todos_path, runtime="codex", content="c",
        )
        todos_store.delete_todo(self.todos_path, todo["id"])
        counts = self.task_count()
        self.assertEqual(counts["total"], 0)
        self.assertEqual(counts["pending"], 0)

    def test_the_list_endpoint_hides_tombstones_by_default(self) -> None:
        alive = todos_store.create_todo(
            self.todos_path, runtime="codex", content="alive",
        )
        gone = todos_store.create_todo(
            self.todos_path, runtime="codex", content="gone",
        )
        todos_store.delete_todo(self.todos_path, gone["id"], deleted_by="u1")
        raw = self.document()

        shaped = _shape_todos("demo", raw)
        ids = [t.id for t in shaped.sessions["_project"].todos]
        self.assertEqual(ids, [alive["id"]])

        with_deleted = _shape_todos("demo", raw, include_deleted=True)
        rows = {t.id: t for t in with_deleted.sessions["_project"].todos}
        self.assertEqual(sorted(rows), sorted([alive["id"], gone["id"]]))
        self.assertIsNotNone(rows[gone["id"]].deleted_at)
        self.assertEqual(rows[gone["id"]].deleted_by, "u1")
        self.assertIsNone(rows[alive["id"]].deleted_at)


class RouteTests(_StoreCase):
    """The endpoints themselves — the three traps T9 names.

    Over HTTP rather than by direct call, because two of the traps live
    in what FastAPI resolves for you: the ``include_deleted`` default
    and the status code. A direct call would hand the route a ``Query``
    object (truthy) as its default and quietly test the opposite.
    """

    PROJECT = "demo"   # the directory _StoreCase creates under the temp root

    def setUp(self) -> None:
        super().setUp()
        app = FastAPI()
        app.include_router(visualizer_router)
        self.client = TestClient(app)
        self.base = f"/api/xo-projects/{self.PROJECT}/todos"

    def create(self, **body) -> dict:
        body.setdefault("runtime", "codex")
        body.setdefault("content", "c")
        response = self.client.post(self.base, json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_the_response_model_accepts_every_field_the_store_writes(self) -> None:
        """Trap (a): ``Todo`` forbids extras, so an undeclared field the
        store adds is a 500 on every read of it."""
        created = self.create(content="ship it")
        self.assertIsNotNone(created["created_at"])
        self.assertIsNone(created["deleted_at"])
        fetched = self.client.get(f"{self.base}/{created['id']}")
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(fetched.json(), created)

    def test_the_list_filters_tombstones_by_default(self) -> None:
        """Trap (b): unfiltered, deleted rows reappear in both UIs. The
        default is what the UIs actually send."""
        alive = self.create(content="alive")
        gone = self.create(content="gone")
        self.client.delete(f"{self.base}/{gone['id']}")

        listed = self.client.get(self.base).json()
        self.assertEqual(
            [t["id"] for t in listed["sessions"]["_project"]["todos"]],
            [alive["id"]],
        )
        everything = self.client.get(
            self.base, params={"include_deleted": "true"}
        ).json()
        rows = {t["id"]: t for t in everything["sessions"]["_project"]["todos"]}
        self.assertEqual(sorted(rows), sorted([alive["id"], gone["id"]]))
        self.assertIsNotNone(rows[gone["id"]]["deleted_at"])

    def test_delete_stays_idempotent_rather_than_404ing(self) -> None:
        """Trap (c): the documented contract is ``deleted: false``, not
        a 404, for a todo that isn't there."""
        todo = self.create()
        first = self.client.delete(f"{self.base}/{todo['id']}")
        second = self.client.delete(f"{self.base}/{todo['id']}")
        missing = self.client.delete(f"{self.base}/nosuchid")
        self.assertEqual(
            [first.status_code, second.status_code, missing.status_code],
            [200, 200, 200],
        )
        self.assertTrue(first.json()["deleted"])
        self.assertFalse(second.json()["deleted"])
        self.assertFalse(missing.json()["deleted"])
        self.assertEqual(missing.json()["todo_id"], "nosuchid")

    def test_a_deleted_todo_is_gone_from_the_single_reads_too(self) -> None:
        todo = self.create()
        self.client.delete(f"{self.base}/{todo['id']}")
        fetched = self.client.get(f"{self.base}/{todo['id']}")
        patched = self.client.patch(
            f"{self.base}/{todo['id']}", json={"status": "completed"}
        )
        for response in (fetched, patched):
            self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(
                response.json()["detail"]["code"], "todo_not_found"
            )

    def test_an_unknown_status_is_still_a_400(self) -> None:
        response = self.client.post(
            self.base,
            json={"runtime": "codex", "content": "c", "status": "in-progress"},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"]["code"], "invalid_status")


if __name__ == "__main__":
    unittest.main()
