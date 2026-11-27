"""The agents list and import (plan B5, B7): ``GET /api/personas``,
``GET /api/personas/importable`` and ``POST /api/personas/import``, and the
one adapter that offers import (claude_code's ``persona_import``). Real files
in a temp projects root, state root and Claude config folder; the jobs and
the project's chat (storm, not running here) are patched."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent.bff.read_host import require_read_host
from routers.cowork_agent.bff.ui_writer import require_ui_writer
from routers.cowork_agent.storm import personas as routes
from services.cowork_agent.adapters.claude_code import persona_import
from services.cowork_agent.storm import personas
from services.project_files import edits

CLAUDE_AGENT = """---
name: reviewer
description: Reviews a diff.
tools: Read, Grep, Glob, Bash(git diff:*), mcp__github__get_pr
model: inherit
---
Read the diff.
"""
LOSSY_AGENT = """---
name: fixer
description: "Fixes: what breaks"
tools: [Read, Edit, Skill]
model: sonnet
permissionMode: acceptEdits
hooks: {PreToolUse: []}
mcpServers: [github, {local: {command: x}}]
---
Fix it.
"""
XO_AGENT = "---\nname: {name}\ndescription: Does a thing.\n---\nDo it.\n"


class ConvertTests(unittest.TestCase):
    def test_a_file_that_maps_fully_drops_nothing_and_parses_clean(self) -> None:
        made = persona_import.convert(CLAUDE_AGENT)
        self.assertEqual((made["name"], made["dropped"]), ("reviewer", []))
        found, problems = personas.parse(made["text"])
        self.assertEqual(problems, [])
        self.assertEqual(found.tools, ("read", "search", "shell:git diff", "mcp:github/get_pr"))
        self.assertEqual((found.model, found.runtime, found.body), (None, persona_import.RUNTIME, "Read the diff."))

    def test_what_xo_cant_carry_is_listed_and_left_out(self) -> None:
        made = persona_import.convert(LOSSY_AGENT)
        self.assertEqual(made["dropped"], ["tools: Skill", "mcpServers: local (inline)", "permissionMode", "hooks"])
        found, problems = personas.parse(made["text"])
        self.assertEqual(problems, [])
        self.assertEqual((found.description, found.tools, found.mcp_servers, found.model),
                         ("Fixes: what breaks", ("read", "edit"), ("github",), "sonnet"))

    def test_a_file_with_no_front_matter_or_name_isnt_an_agent(self) -> None:
        for text in ("Just text.", "---\ndescription: x\n---\nbody"):
            with self.assertRaises(ValueError):
                persona_import.convert(text)


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.project = base / "projects" / "demo"
        (self.project / ".xo").mkdir(parents=True)
        self.state = base / "state"
        self.claude = base / "claude"
        env = mock.patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(base / "projects"),
                                           "QUIRQ_STATE_ROOT": str(self.state), "CLAUDE_CONFIG_DIR": str(self.claude)})
        env.start()
        self.addCleanup(env.stop)
        edits._pruned_at.clear()
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[require_read_host] = lambda: None
        app.dependency_overrides[require_ui_writer] = lambda: None
        self.client = TestClient(app)

    @staticmethod
    def put(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


class ListTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        jobs = [{"id": "j1", "name": "Mine", "chat": "chat_demo", "persona": {"name": "reviewer", "level": "project"}},
                {"id": "j2", "name": "Theirs", "chat": "chat_other",
                 "persona": {"name": "reviewer", "level": "project"}},
                {"id": "j3", "name": "Space", "chat": "chat_other", "persona": {"name": "writer", "level": "space"}},
                {"id": "j4", "name": "None", "chat": "chat_demo", "persona": None}]

        async def views():
            return jobs

        async def project(name):
            return {"id": "chat_demo"}
        for patcher in (mock.patch.object(routes.jobs, "views", views, create=True),
                        mock.patch.object(routes.chats, "current", lambda: SimpleNamespace(project=project))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_each_agent_with_its_level_shadowing_jobs_problems_and_the_may_choices(self) -> None:
        self.put(self.project / ".xo" / "agents" / "reviewer.md", XO_AGENT.format(name="reviewer"))
        self.put(self.state / "agents" / "reviewer.md", XO_AGENT.format(name="reviewer"))
        self.put(self.state / "agents" / "writer.md", XO_AGENT.format(name="writer") + "\n")
        self.put(self.state / "agents" / "broken.md", "---\nname: broken\ncolour: red\n---\n")
        got = self.client.get("/api/personas", params={"project": "demo"}).json()
        self.assertEqual(got["may"], personas.MAY)
        by = {(p["name"], p["level"]): p for p in got["personas"]}
        self.assertEqual(sorted(by), [("broken", "space"), ("reviewer", "project"), ("reviewer", "space"),
                                      ("writer", "space")])
        mine = by["reviewer", "project"]
        self.assertEqual((mine["path"], mine["shadows"], mine["used_by"]),
                         (".xo/agents/reviewer.md", {"name": "reviewer", "level": "space"},
                          {"jobs": [{"id": "j1", "name": "Mine"}]}))
        self.assertEqual(by["reviewer", "space"]["shadowed"], {"name": "reviewer", "level": "project"})
        self.assertEqual(by["writer", "space"]["used_by"], {"jobs": [{"id": "j3", "name": "Space"}]})
        self.assertEqual({p["key"] for p in by["broken", "space"]["problems"]}, {"colour", "description"})
        self.assertEqual(set(mine), {"name", "description", "level", "path", "version", "runtime", "model", "effort",
                                     "tools", "disallowedTools", "mcpServers", "shadows", "shadowed", "used_by",
                                     "problems"})

    def test_without_a_project_the_spaces_agents(self) -> None:
        self.put(self.project / ".xo" / "agents" / "reviewer.md", XO_AGENT.format(name="reviewer"))
        self.put(self.state / "agents" / "writer.md", XO_AGENT.format(name="writer"))
        got = self.client.get("/api/personas").json()
        self.assertEqual([(p["name"], p["level"]) for p in got["personas"]], [("writer", "space")])


class ImportTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.claude / "agents" / "reviewer.md", CLAUDE_AGENT)
        self.put(self.claude / "agents" / "fixer.md", LOSSY_AGENT)
        self.put(self.claude / "agents" / "notes.md", "Not an agent.")
        self.put(self.project / ".claude" / "agents" / "local.md", CLAUDE_AGENT.replace("reviewer", "local"))

    def importable(self, project: str | None = "demo") -> dict:
        got = self.client.get("/api/personas/importable", params={"project": project} if project else {})
        self.assertEqual(got.status_code, 200, got.text)
        return {(i["adapter"], i["id"]): i for i in got.json()["importable"]}

    def test_every_adapter_offers_its_files_each_checked_on_its_own(self) -> None:
        found = self.importable()
        mine = {k: v for k, v in found.items() if v["from"] == persona_import.DISPLAY_NAME}
        self.assertEqual(sorted(i["id"] for i in mine.values()), ["project:local", "space:fixer", "space:reviewer"])
        reviewer = next(i for i in mine.values() if i["id"] == "space:reviewer")
        self.assertEqual((reviewer["level"], reviewer["dropped"], reviewer["problems"]), ("space", [], []))
        self.assertEqual(next(i for i in mine.values() if i["id"] == "space:fixer")["dropped"][-2:],
                         ["permissionMode", "hooks"])
        self.assertNotIn("project:local", {i["id"] for i in self.importable(None).values()})

    def import_(self, id_: str, **body) -> dict:
        got = self.client.post("/api/personas/import", json={"adapter": self.adapter(id_), "id": id_, **body})
        return {"status": got.status_code, **got.json()}

    def adapter(self, id_: str) -> str:
        return next(a for a, i in self.importable() if i == id_)

    def test_a_file_that_maps_fully_imports_as_it_is_into_the_space(self) -> None:
        made = self.import_("space:reviewer")
        self.assertEqual(made["status"], 200, made)
        self.assertEqual((made["relative_path"], made["dropped"]), ("reviewer.md", []))
        [saved] = [p for p in personas.listing(None) if p.name == "reviewer"]
        self.assertEqual((saved.level, saved.problems, saved.version), ("space", (), made["version"]))
        again = self.import_("space:reviewer")
        self.assertEqual((again["status"], again["detail"]["code"]), (409, "file_exists"))

    def test_a_file_with_settings_xo_cant_carry_imports_only_without_them(self) -> None:
        refused = self.import_("space:fixer")
        self.assertEqual((refused["status"], refused["detail"]["code"]), (409, "settings_dropped"))
        self.assertIn("permissionMode", refused["detail"]["what"])
        self.assertFalse((self.state / "agents" / "fixer.md").exists())
        made = self.import_("space:fixer", without_dropped=True)
        self.assertEqual(made["status"], 200, made)
        self.assertNotIn("permissionMode", (self.state / "agents" / "fixer.md").read_text())

    def test_a_projects_own_agent_imports_into_its_agents_folder(self) -> None:
        made = self.import_("project:local", project="demo")
        self.assertEqual((made["status"], made["relative_path"]), (200, ".xo/agents/local.md"))
        self.assertEqual(personas.resolve("demo", "local", "project").problems, ())

    def test_an_id_no_adapter_offers_is_404(self) -> None:
        got = self.client.post("/api/personas/import", json={"adapter": "nobody", "id": "space:x"})
        self.assertEqual((got.status_code, got.json()["detail"]["code"]), (404, "not_importable"))


if __name__ == "__main__":
    unittest.main()
