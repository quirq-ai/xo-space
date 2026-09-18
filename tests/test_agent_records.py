"""One agent record per project, and each backend answers only for its own.

``<project>/.xo/agent.json`` is written by the adapters that keep their agent
record in the project. ``GET /api/agents`` lists the active backend's world, and
``get_detail``/``patch`` answer only for records their backend owns (or untagged
ones, which predate the tag). These tests hold every such adapter to that:
another backend's record is not listed and is never overwritten by a create,
and neither is a record that does not parse.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from routers.cowork_agent.agents import CreateAgentBody
from services import xo_structure
from services.cowork_agent import coder_identity
from services.cowork_agent.adapters.loader import list_capability_providers, try_load_capability

PROJECT = "shared-project"


def _record_writers() -> list[str]:
    return [
        name for name in list_capability_providers("agents")
        if hasattr(try_load_capability("agents", agent=name), "_meta_path")
    ]


class AgentRecordOwnershipTests(unittest.TestCase):
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
        owner = patch.object(coder_identity, "resolve_user_id", return_value="local")
        owner.start()
        self.addCleanup(owner.stop)
        xo_structure._CHECKED.clear()
        self.addCleanup(xo_structure._CHECKED.clear)
        self.writers = _record_writers()
        self.assertTrue(self.writers, "no adapter writes .xo/agent.json")

    def record(self, text: str) -> Path:
        path = try_load_capability("agents", agent=self.writers[0])._meta_path(PROJECT)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    @staticmethod
    def listed(mod) -> list[str]:
        return [row["name"] for row in mod.list_agents()]

    def test_another_backends_record_is_not_listed_opened_or_overwritten(self) -> None:
        path = self.record(json.dumps({"id": PROJECT, "name": "Theirs", "backend": "someone_else"}))
        before = path.read_bytes()
        for name in self.writers:
            with self.subTest(adapter=name):
                mod = try_load_capability("agents", agent=name)
                self.assertEqual(self.listed(mod), [])
                self.assertIsNone(mod.get_detail(PROJECT))
                response = mod.create_agent(CreateAgentBody(name="Mine", id=PROJECT))
                self.assertEqual(response.status_code, 409)
                self.assertIn("someone_else", json.loads(response.body)["detail"])
                self.assertEqual(path.read_bytes(), before)

    def test_each_backend_lists_its_own_record_and_not_the_others(self) -> None:
        for owner in self.writers:
            with self.subTest(owner=owner):
                path = self.record(json.dumps({"id": PROJECT, "name": "Owned", "backend": owner}))
                for name in self.writers:
                    mod = try_load_capability("agents", agent=name)
                    self.assertEqual(self.listed(mod), [PROJECT] if name == owner else [])
                path.unlink()

    def test_an_untagged_record_is_listed_by_every_backend_and_kept(self) -> None:
        path = self.record(json.dumps({"id": PROJECT, "name": "Old"}))
        before = path.read_bytes()
        for name in self.writers:
            with self.subTest(adapter=name):
                mod = try_load_capability("agents", agent=name)
                self.assertEqual(self.listed(mod), [PROJECT])
                self.assertEqual(mod.create_agent(CreateAgentBody(name="New", id=PROJECT)).status_code, 409)
                self.assertEqual(path.read_bytes(), before)

    def test_a_record_that_does_not_parse_is_never_overwritten(self) -> None:
        path = self.record("{not json")
        for name in self.writers:
            with self.subTest(adapter=name):
                mod = try_load_capability("agents", agent=name)
                response = mod.create_agent(CreateAgentBody(name="New", id=PROJECT))
                self.assertEqual(response.status_code, 409)
                self.assertIn("cannot be read", json.loads(response.body)["detail"])
                self.assertEqual(path.read_text(encoding="utf-8"), "{not json")


if __name__ == "__main__":
    unittest.main()
