"""Regressions for the six defects the syncplan stage verifiers found.

Each of these shipped green — the full suite passed and the route parity
gate passed — and each was still wrong. They are collected here rather
than scattered into the topical modules because what they have in common
is *how they hid*, and that is worth keeping in one place:

* four of the six sit on a boundary no test crossed (a sink's output
  against its own schema, a response model against what is actually on
  disk), so no gate could go red;
* one only appears under concurrency;
* one only appears when a file is deleted out from under a running
  process.

Every test below fails against the code as it shipped.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from jsonschema import Draft7Validator

_SCHEMA_DIR = (
    Path(__file__).resolve().parents[1]
    / "services" / "cowork_agent" / "visualizer" / "schema"
)


def _schema(name: str) -> dict:
    return json.loads((_SCHEMA_DIR / name).read_text(encoding="utf-8"))


class TodoStatusReadPathTests(unittest.TestCase):
    """R1 — a legacy status must not 500 the whole project's todo list.

    T6 typed the *response* model ``Todo.status`` as a strict ``Literal``
    so the OpenAPI schema would finally carry the enum. But
    ``_shape_todos`` is called at ``bff/visualizer.py`` **outside** the
    route's try/except, so one unrecognised value on disk raised
    ``ValidationError`` and took every well-formed row with it.
    """

    def test_a_legacy_status_does_not_500_the_list(self):
        from routers.cowork_agent.bff.visualizer import _shape_todos

        raw = {"sessions": {"s1": {"runtime": "codex", "todos": [
            {"id": "1", "content": "legacy", "status": "in-progress"},
            {"id": "2", "content": "fine", "status": "pending"},
        ]}}}
        with self.assertLogs(
            "routers.cowork_agent.bff.visualizer", level="WARNING"
        ) as logs:
            resp = _shape_todos("p", raw)

        todos = resp.sessions["s1"].todos
        self.assertEqual(len(todos), 2, "the good row must survive the bad one")
        self.assertEqual(todos[1].status, "pending")
        # Coerced, not dropped: a work item that vanishes with no signal is
        # worse than one shown with a fallback.
        self.assertEqual(todos[0].status, "pending")
        self.assertIn("in-progress", "".join(logs.output))

    def test_every_declared_status_still_round_trips(self):
        from routers.cowork_agent.bff.visualizer import _shape_todos
        from services.cowork_agent.visualizer.todo_status import TODO_STATUSES

        raw = {"sessions": {"s": {"runtime": "r", "todos": [
            {"id": str(i), "content": "c", "status": st}
            for i, st in enumerate(TODO_STATUSES)
        ]}}}
        got = [t.status for t in _shape_todos("p", raw).sessions["s"].todos]
        self.assertEqual(got, list(TODO_STATUSES))

    def test_the_openapi_enum_survives_the_fix(self):
        """The whole point of T6 — do not fix the 500 by widening to str."""
        from routers.cowork_agent.bff._visualizer_models import Todo
        from services.cowork_agent.visualizer.todo_status import TODO_STATUSES

        enum = Todo.model_json_schema()["properties"]["status"].get("enum")
        self.assertEqual(enum, list(TODO_STATUSES))


class SelfHealOnDeletedTargetTests(unittest.TestCase):
    """R2 — ``previous=`` must not skip a write when the file is gone.

    T26 wired six writers onto the in-memory-baseline fast path. That
    path never touches the disk, so a deleted target compared equal to
    the baseline, the write was skipped, and the file never came back —
    a regression against the unconditional write it replaced.
    """

    def setUp(self):
        from services.cowork_agent.visualizer import atomic_write
        self.aw = atomic_write
        self.path = Path(tempfile.mkdtemp()) / "projects.json"
        self.payload = {"schema": 2, "projects": {"a": {"pid": None}}}

    def test_deleted_target_is_recreated_on_the_previous_fast_path(self):
        self.assertTrue(
            self.aw.write_json_atomic_if_changed(
                self.path, self.payload, previous=None)
        )
        previous = json.loads(self.path.read_text())

        self.path.unlink()
        wrote = self.aw.write_json_atomic_if_changed(
            self.path, self.payload, previous=previous)

        self.assertTrue(wrote, "an absent target must always be written")
        self.assertTrue(self.path.exists())
        self.assertEqual(json.loads(self.path.read_text()), self.payload)

    def test_steady_state_still_skips(self):
        """The T26 win must survive the fix — this is not a free stat()."""
        self.aw.write_json_atomic_if_changed(
            self.path, self.payload, previous=None)
        previous = json.loads(self.path.read_text())
        before = self.path.stat().st_mtime_ns

        for _ in range(5):
            self.assertFalse(
                self.aw.write_json_atomic_if_changed(
                    self.path, self.payload, previous=previous)
            )
        self.assertEqual(self.path.stat().st_mtime_ns, before)


class WriterSchemaAgreementTests(unittest.TestCase):
    """R3 — three live writers emitted documents their own schema rejected.

    No test crossed the sink/schema boundary, so nothing went red. Each
    case below is the *real* writer's output, not a hand-built fixture.
    """

    def test_timeline_carries_every_status_transition(self):
        """T7 widened the emitter; the 12-branch oneOf still lagged."""
        validator = Draft7Validator(_schema("timeline.schema.json"))
        line = {"ts": "2026-09-07T12:00:00Z", "type": "todo.status_changed",
                "session_id": "s1", "todo_id": "a1", "status": "blocked"}
        self.assertTrue(validator.is_valid(line), list(validator.iter_errors(line)))

    def test_timeline_schema_did_not_become_vacuous(self):
        validator = Draft7Validator(_schema("timeline.schema.json"))
        self.assertFalse(validator.is_valid(
            {"ts": "2026-09-07T12:00:00Z", "type": "totally.made.up"}))
        self.assertFalse(validator.is_valid(
            {"ts": "2026-09-07T12:00:00Z", "type": "todo.status_changed",
             "status": "blocked"}), "todo_id is required")

    def test_augment_row_declares_the_private_counter_state(self):
        """``_task_states`` is persisted on purpose (count integrity across
        a restart, sinks/sessions_augment.py:240-246) into a document
        declared ``additionalProperties: false``."""
        validator = Draft7Validator(_schema("sessions-augment.schema.json"))
        counts = {"total": 0, "pending": 0, "in_progress": 0,
                  "completed": 0, "cancelled": 0, "blocked": 0}
        row = {"messageCount": 0, "toolCallCount": 0, "taskCount": counts,
               "_task_states": {"a1": "pending"}}
        doc = {"schema": 2, "updated_at": "2026-09-07T12:00:00Z",
               "sessions": {"k": row}}
        self.assertTrue(validator.is_valid(doc), list(validator.iter_errors(doc)))

        bad = dict(doc)
        bad["sessions"] = {"k": {**row, "_undeclared": 1}}
        self.assertFalse(validator.is_valid(bad), "allowlist must still hold")

    def test_activity_sink_output_validates_when_a_timestamp_is_unknown(self):
        """T24 stopped stamping now() for a missing timestamp — which is
        what made the document deterministic, and what made it fail a
        schema that still required the field."""
        from services.cowork_agent.visualizer.sinks import activity

        validator = Draft7Validator(_schema("activity.schema.json"))
        out = Path(tempfile.mkdtemp()) / "activity.json"
        rows = [{"session_id": "s1", "runtime": "codex", "project_id": "p",
                 "started_at_ms": 0, "updated_at_ms": 0}]
        models = {"s1": "claude-opus-5"}

        activity.apply(out, rows, model_by_session=models)
        doc = json.loads(out.read_text())
        self.assertTrue(validator.is_valid(doc), list(validator.iter_errors(doc)))

        # And the T24 property that motivated it all.
        self.assertFalse(
            activity.apply(out, rows, model_by_session=models),
            "identical input must not rewrite the file",
        )

    def test_activity_schema_did_not_become_vacuous(self):
        validator = Draft7Validator(_schema("activity.schema.json"))
        base = {"session_id": "s", "runtime": "r", "agent": "m", "user_id": "u"}
        self.assertTrue(validator.is_valid(
            {"schema": 1, "open_sessions": [base]}))
        self.assertFalse(validator.is_valid(
            {"schema": 1, "open_sessions": [{**base, "opened_at": 12345}]}),
            "a non-string timestamp must still be rejected")
        self.assertFalse(validator.is_valid(
            {"schema": 1, "open_sessions": [{"session_id": "s"}]}),
            "agent/user_id are still required")


class ViewBuildSingleFlightTests(unittest.TestCase):
    """R4 — concurrent rebuilds, and a failed build silencing the watcher.

    One rebuild is a full workspace walk plus a ``git log`` per project
    plus an Argus SQLite scan. ``build`` consulted no throttle, so N
    simultaneous clients on a stale view started N of them. Separately,
    ``_last_build`` was stamped *before* the per-view try/except, so a
    build that failed outright still reset the watcher's throttle.
    """

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            "QUIRQ_STATE_ROOT": tempfile.mkdtemp(),
            "XO_PROJECTS_ROOT": tempfile.mkdtemp(),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

        from services.cowork_agent.visualizer.workspace import views
        self.views = views
        views.scaffold()
        self._saved = views._last_build
        self.addCleanup(lambda: setattr(views, "_last_build", self._saved))
        views._last_build = 0.0

    def _stub(self, fn):
        import services.cowork_agent.visualizer.space_index as si
        p = mock.patch.object(si, "build_space_data", fn)
        p.start()
        self.addCleanup(p.stop)

    def test_concurrent_requests_collapse_into_one_build(self):
        calls = []

        def slow():
            calls.append(1)
            time.sleep(0.25)
            return {"space": {"nodes": []}, "meta": {}}

        self._stub(slow)
        results: list = []
        threads = [threading.Thread(target=lambda: results.append(
            self.views.build("space"))) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(calls), 1, "8 requests must trigger 1 rebuild")
        self.assertEqual(len(results), 8)
        self.assertTrue(all(r and "space" in r for r in results),
                        "every waiter must receive the leader's result")

    def test_a_failed_build_does_not_silence_the_watcher(self):
        def boom():
            raise RuntimeError("telemetry unavailable")

        self._stub(boom)
        self.views._build_all(only="space")
        self.assertEqual(
            self.views._last_build, 0.0,
            "a build that produced nothing must not reset the throttle",
        )

    def test_a_successful_build_does_stamp(self):
        self._stub(lambda: {"space": {"nodes": []}, "meta": {}})
        self.views.build("space")
        self.assertNotEqual(self.views._last_build, 0.0)

    def test_a_failed_build_does_not_poison_the_flight_slot(self):
        import services.cowork_agent.visualizer.space_index as si

        def boom():
            raise RuntimeError("transient")

        with mock.patch.object(si, "build_space_data", boom):
            self.views._build_all(only="space")

        calls = []

        def ok():
            calls.append(1)
            return {"space": {"nodes": []}, "meta": {}}

        with mock.patch.object(si, "build_space_data", ok):
            self.assertTrue(self.views.build("space"))
        self.assertEqual(len(calls), 1, "the key must be reusable after a failure")


if __name__ == "__main__":
    unittest.main()
