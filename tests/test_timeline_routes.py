"""Timeline lines carry ``pid``, and the timeline routes must serve them.

``TimelineEvent`` refuses unknown fields. A field added to the lines but not to
the model makes both timeline routes drop every new line without an error,
which is what happened when ``pid`` was first added.
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

from routers.cowork_agent.bff import visualizer as project_routes
from routers.cowork_agent.bff import workspace_visualizer as space_routes
from routers.cowork_agent.bff._visualizer_models import TimelineEvent
from services.cowork_agent import project_layout

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "demo"
PID = "7deb4a22-0789-497d-9399-a2272579fa06"
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
        xo = base / "projects" / PROJECT / ".xo"
        xo.mkdir(parents=True)
        (xo / "project.json").write_text(json.dumps({
            "schema": 2, "pid": PID, "name": PROJECT,
            "owner_user_id": "local", "created_at": "2026-09-14T10:00:00Z",
        }), encoding="utf-8")
        runtime = project_layout.runtime_dir_for_project(PROJECT, create=True)
        (runtime / "timeline.jsonl").write_text(json.dumps(LINE) + "\n", encoding="utf-8")
        project_layout.workspace_timeline_path().write_text(
            json.dumps({**LINE, "project_id": PROJECT}) + "\n", encoding="utf-8",
        )
        app = FastAPI()
        app.include_router(project_routes.router)
        app.include_router(space_routes.router)
        self.client = TestClient(app)

    def test_the_project_timeline_serves_lines_with_a_pid(self) -> None:
        response = self.client.get(f"/api/xo-projects/{PROJECT}/timeline")
        self.assertEqual(response.status_code, 200, response.text)
        [event] = response.json()["events"]
        self.assertEqual(event["pid"], PID)

    def test_the_space_timeline_serves_lines_with_a_pid(self) -> None:
        response = self.client.get("/api/xo-projects/timeline")
        self.assertEqual(response.status_code, 200, response.text)
        [event] = response.json()["events"]
        self.assertEqual((event["pid"], event["project_id"]), (PID, PROJECT))

    def test_the_model_accepts_every_field_the_timeline_schema_declares(self) -> None:
        schema = json.loads(
            (ROOT / "services" / "cowork_agent" / "visualizer" / "schema" / "timeline.schema.json")
            .read_text(encoding="utf-8")
        )
        fields = getattr(TimelineEvent, "model_fields", None) or TimelineEvent.__fields__
        self.assertLessEqual(set(schema["propertyNames"]["enum"]), set(fields))


if __name__ == "__main__":
    unittest.main()
