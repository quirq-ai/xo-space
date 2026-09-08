"""``<XO root>/.xo/space.json`` — the Space record (syncplan §5.3, T14).

This path used to hold the derived d3 graph, which is rebuilt by two writers
that share no lock, so anything written beside it was destroyed on the next
rebuild. The graph moved to ``~/.quirq/workspace/graph.json``; ``GET
/xo/space.json`` still serves it, unchanged, because the URL is a name and not
a path. What is left behind is a durable record with exactly one writer.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.visualizer.workspace import space_json, views


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_FILE = (
    ROOT / "services" / "cowork_agent" / "visualizer" / "schema" / "space.schema.json"
)


def _workspace(tmp: str) -> Path:
    root = Path(tmp) / "projects"
    for name in ("alpha", "beta"):
        (root / name / ".xo").mkdir(parents=True)
        (root / name / ".xo" / "project.json").write_text(
            json.dumps({"schema": 1, "name": name}), encoding="utf-8"
        )
        (root / name / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    return root


def _env(tmp: str, **extra: str) -> dict:
    return {
        "XO_PROJECTS_ROOT": str(Path(tmp) / "projects"),
        "QUIRQ_STATE_ROOT": str(Path(tmp) / ".quirq"),
        "HOME": tmp,
        **extra,
    }


class SpaceRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        space_json._last_build = 0.0

    def _apply_now(self) -> dict:
        """Apply and read back. **Must** be called inside a patched
        environment: ``space_json.path()`` resolves ``XO_PROJECTS_ROOT`` on
        every call, so a read outside the patch reaches the real workspace."""
        space_json._last_build = 0.0
        space_json.apply()
        return json.loads(space_json.path().read_text(encoding="utf-8"))

    def _apply(self, tmp: str, **extra: str) -> dict:
        with patch.dict(os.environ, _env(tmp, **extra), clear=False):
            return self._apply_now()

    @staticmethod
    def _rewrite(mutate) -> None:
        """Edit the record on disk the way a user or a later writer would."""
        path = space_json.path()
        doc = json.loads(path.read_text(encoding="utf-8"))
        mutate(doc)
        path.write_text(json.dumps(doc), encoding="utf-8")

    def test_the_record_replaces_the_graph_at_this_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            record = self._apply(tmp, CODER_WORKSPACE_ID="ws-42")

        self.assertEqual(record["schema"], 2)
        self.assertEqual(record["$schema"], "xo/space.schema.json")
        self.assertEqual(record["space_id"], "ws-42")
        self.assertIn("roots", record)
        self.assertIn("agents", record)
        # the document that used to live here
        for graph_key in ("hubs", "leaves", "ties", "categories"):
            self.assertNotIn(graph_key, record)

    def test_space_id_is_captured_from_the_environment_never_minted(self) -> None:
        """syncplan O1: Coder-only. Off Coder the field is null, and no
        fallback is ever generated — a locally minted id would diverge from
        the externally assigned one the moment the Space ran on Coder."""
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            env = dict(os.environ)
            env.pop("CODER_WORKSPACE_ID", None)
            with patch.dict(os.environ, env, clear=True):
                record = self._apply(tmp)
                self.assertIsNone(record["space_id"])

                # and it is picked up, not invented, when it appears
                record = self._apply(tmp, CODER_WORKSPACE_ID="ws-99")
                self.assertEqual(record["space_id"], "ws-99")

    def test_the_roots_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            record = self._apply(tmp)
            self.assertEqual(
                record["roots"],
                {
                    "projects_root": str(Path(tmp) / "projects"),
                    "state_root": str(Path(tmp) / ".quirq"),
                },
            )

    def test_the_agent_roster_is_discovered_and_carries_its_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                first = self._apply_now()
                names = [row["name"] for row in first["agents"]]
                self.assertEqual(names, sorted(names))
                self.assertTrue(names, "expected at least one discovered agent")
                for row in first["agents"]:
                    self.assertIn("binary_available", row)
                    self.assertIsInstance(row["capabilities"], list)
                    self.assertIsNotNone(row["first_seen_at"])

                # first_seen_at is set once and carried forward
                def age_it(doc: dict) -> None:
                    doc["agents"][0]["first_seen_at"] = "2020-01-01T00:00:00Z"

                self._rewrite(age_it)
                again = self._apply_now()

        self.assertEqual(again["agents"][0]["first_seen_at"], "2020-01-01T00:00:00Z")

    def test_last_used_at_does_not_churn_the_file_every_tick(self) -> None:
        """A per-tick stamp on the active agent would rewrite this file once
        a second forever, which is the churn the record exists to avoid."""
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                space_json._last_build = 0.0
                self.assertTrue(space_json.apply())
                stamp = space_json.path().stat().st_mtime_ns
                for _ in range(3):
                    space_json._last_build = 0.0
                    self.assertFalse(space_json.apply())
                self.assertEqual(space_json.path().stat().st_mtime_ns, stamp)

    def test_the_sink_self_throttles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            with patch.dict(
                os.environ, _env(tmp, XO_SPACE_REFRESH_S="3600"), clear=False
            ):
                space_json._last_build = 0.0
                self.assertTrue(space_json.apply())     # first pass writes
                self.assertFalse(space_json.apply())    # second is not due
                # force skips the throttle, not the change gate
                self.assertFalse(space_json.apply(force=True))

    def test_the_label_is_seeded_once_and_then_belongs_to_the_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            env = _env(tmp, CODER_WORKSPACE_NAME="my-space")
            with patch.dict(os.environ, env, clear=False):
                record = self._apply_now()
                self.assertEqual(record["label"], "my-space")

                def edit(doc: dict) -> None:
                    doc["label"] = "renamed by hand"
                    doc["a_future_writers_key"] = {"kept": True}

                self._rewrite(edit)
                record = self._apply_now()

        # the sink owns neither key: R-WRITE, via write_json_owned
        self.assertEqual(record["label"], "renamed by hand")
        self.assertEqual(record["a_future_writers_key"], {"kept": True})

    def test_a_resolved_owner_is_never_downgraded_to_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                self._apply_now()
                self._rewrite(lambda doc: doc.update(owner_user_id="u-1234"))
                record = self._apply_now()

        self.assertEqual(record["owner_user_id"], "u-1234")

    def test_a_pre_t14_graph_at_this_path_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            (root / ".xo").mkdir(parents=True, exist_ok=True)
            (root / ".xo" / "space.json").write_text(
                json.dumps({"meta": {"title": "Space"}, "hubs": [], "leaves": []}),
                encoding="utf-8",
            )
            record = self._apply(tmp)

        self.assertEqual(record["schema"], 2)
        self.assertNotIn("hubs", record)

    def test_an_unreadable_record_is_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            (root / ".xo").mkdir(parents=True, exist_ok=True)
            (root / ".xo" / "space.json").write_text("{not json", encoding="utf-8")
            record = self._apply(tmp)

        self.assertEqual(record["schema"], 2)

    def test_the_writer_emits_exactly_what_the_schema_declares(self) -> None:
        schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            record = self._apply(tmp, CODER_WORKSPACE_NAME="my-space")

        self.assertEqual(schema["$id"], "xo/space.schema.json")
        self.assertEqual(sorted(record), sorted(schema["properties"]))
        for key in schema["required"]:
            self.assertIn(key, record)
        agent_schema = schema["properties"]["agents"]["items"]["properties"]
        for row in record["agents"]:
            self.assertEqual(sorted(row), sorted(agent_schema))


class GraphRouteIsUnchangedTests(unittest.TestCase):
    """T14's acceptance: the route serves the same payload it always did,
    from a different file, while the record keeps its own."""

    def setUp(self) -> None:
        space_json._last_build = 0.0
        views._last_build = 0.0

    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers.xo_data import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_space_json_still_serves_the_graph_now_from_the_runtime_tier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                from services.cowork_agent.visualizer.space_index import (
                    build_space_data,
                )

                expected = build_space_data()
                with self._client() as client:
                    served = client.get("/xo/space.json")
                self.assertEqual(served.status_code, 200)
                payload = served.json()

                graph_file = views.graph_path()
                self.assertEqual(
                    graph_file, Path(tmp) / ".quirq" / "workspace" / "graph.json"
                )
                on_disk = json.loads(graph_file.read_text(encoding="utf-8"))

                # the route answers with the graph, byte for byte the
                # builder's payload, and the file it reads is the new one
                self.assertEqual(payload["hubs"], expected["hubs"])
                self.assertEqual(payload["leaves"], expected["leaves"])
                self.assertEqual(payload, on_disk)

                # and the request path did not put the graph back into .xo
                self.assertFalse((root / ".xo" / "space.json").exists())

    def test_the_record_survives_a_graph_rebuild(self) -> None:
        """The whole reason for the split: two unlocked writers rebuild the
        graph, so identity kept beside it was destroyed on the next tick."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            with patch.dict(
                os.environ, _env(tmp, CODER_WORKSPACE_ID="ws-77"), clear=False
            ):
                space_json._last_build = 0.0
                space_json.apply()

                # both graph writers: the watcher's sink and a request thread
                views._last_build = 0.0
                views.apply(force=True)
                with self._client() as client:
                    client.get("/xo/space.json")

                record = json.loads(
                    (root / ".xo" / "space.json").read_text(encoding="utf-8")
                )

        self.assertEqual(record["space_id"], "ws-77")
        self.assertEqual(record["schema"], 2)


if __name__ == "__main__":
    unittest.main()
