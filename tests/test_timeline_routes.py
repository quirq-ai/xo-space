"""Timeline lines carry ``pid``, and the timeline routes must serve them.

``TimelineEvent`` types the envelope (``ts``, ``type``, then ``pid``,
``project_id``, ``session_id``, ``runtime``) and lets every other key through:
the types are open (a module declares its own in ``events.TYPES``), so a key
the model did not name must never make the routes drop the line, which is
what happened when ``pid`` was first added and the model still forbade
extras.
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

from modules.projects import routes as project_routes
from modules.timeline import service as timeline_service
from routers.errors import install_service_errors
from routers.cowork_agent.bff._visualizer_models import TimelineEvent
from services.cowork_agent import project_layout

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "services" / "cowork_agent" / "visualizer" / "schema" / "timeline.schema.json"
PROJECT = "demo"
PID = "7deb4a22-0789-497d-9399-a2272579fa06"
ENVELOPE = ("ts", "type", "pid", "project_id", "session_id", "runtime")
LINE = {
    "ts": "2026-09-14T13:52:49Z", "type": "todo.added", "pid": PID,
    "session_id": "_project", "runtime": "r",
    "todo": {"id": "abcd1234", "content": "x", "status": "pending"},
}


class TimelineRouteTests(unittest.TestCase):
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
        xo = base / "projects" / PROJECT / ".xo"
        xo.mkdir(parents=True)
        (xo / "project.json").write_text(json.dumps({
            "schema": 2, "pid": PID, "name": PROJECT,
            "owner_user_id": "local", "created_at": "2026-09-14T10:00:00Z",
        }), encoding="utf-8")
        runtime = project_layout.runtime_dir_for_project(PROJECT, create=True)
        (runtime / "timeline.jsonl").write_text(json.dumps(LINE) + "\n", encoding="utf-8")
        # The copy an older install put in the Space log: counted once.
        project_layout.workspace_timeline_path().write_text(
            json.dumps({**LINE, "project_id": PROJECT}) + "\n", encoding="utf-8",
        )
        app = FastAPI()
        app.include_router(project_routes.router)  # the project and the Space timeline, one router
        install_service_errors(app)
        self.client = TestClient(app)

    def test_the_project_timeline_serves_lines_with_a_pid(self) -> None:
        response = self.client.get(f"/api/xo-projects/{PROJECT}/timeline")
        self.assertEqual(response.status_code, 200, response.text)
        [event] = response.json()["events"]
        self.assertEqual(event["pid"], PID)
        self.assertEqual(event["todo"], LINE["todo"], "a key outside the envelope passes through")

    def test_the_space_timeline_serves_lines_with_a_pid(self) -> None:
        response = self.client.get("/api/xo-projects/timeline")
        self.assertEqual(response.status_code, 200, response.text)
        [event] = response.json()["events"]
        self.assertEqual((event["pid"], event["project_id"]), (PID, PROJECT))

    def test_the_model_types_the_envelope_the_schema_declares(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(schema["required"], ["ts", "type"])
        self.assertIs(schema["additionalProperties"], True, "the keys after the envelope belong to the module")
        for name in ENVELOPE:
            self.assertEqual(schema["properties"][name]["type"], "string")

        fields = TimelineEvent.model_fields
        self.assertTrue(set(ENVELOPE) <= set(fields))
        self.assertTrue(fields["ts"].is_required())
        self.assertTrue(fields["type"].is_required())
        for name in ENVELOPE[2:]:
            self.assertFalse(fields[name].is_required(), f"{name} is optional on the wire")
        self.assertEqual(TimelineEvent.model_config.get("extra"), "allow")
        event = TimelineEvent(**LINE, someone_elses_key={"n": 1})
        self.assertEqual(event.model_dump()["someone_elses_key"], {"n": 1})
        with self.assertRaises(Exception):
            TimelineEvent(type="todo.added")

    def test_the_schema_enum_is_within_the_declared_types(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        enum = schema["properties"]["type"]["enum"]
        self.assertIn("todo.added", enum)
        self.assertEqual(len(enum), len(set(enum)))
        self.assertTrue(set(enum) <= timeline_service.declared_types())


if __name__ == "__main__":
    unittest.main()
