"""Agents a person defines (storm/personas.py), and their two folders in the
project-files code: a project's ``.xo/agents/`` and the Space's
``~/.quirq/agents/`` under ``SPACE_ID``. Real files in a temp projects root
and a temp state root."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from services.cowork_agent.storm import personas
from services.project_files import edits, ops, read, save, versions
from services.project_files.address import SPACE_ID, FileRefused, Place, classify, is_noise, locate, version_of

GOOD = """---
name: reviewer
description: Reviews a diff and says what breaks.
tools: read, search, shell:git diff
disallowedTools: [web_fetch]
model: some-model
effort: high
mcpServers: [github]
---
Read the diff. Say what breaks, with the line.
"""


def agent(name: str, description: str = "Does a thing.", extra: str = "") -> str:
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\nDo it.\n"


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.projects = self.base / "projects"
        self.project = self.projects / "demo"
        (self.project / ".xo").mkdir(parents=True)
        self.state = self.base / "state"
        env = mock.patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(self.projects),
                                           "QUIRQ_STATE_ROOT": str(self.state)})
        env.start()
        self.addCleanup(env.stop)
        edits._pruned_at.clear()

    def put_project(self, name: str, text: str) -> Path:
        path = self.project / ".xo" / "agents" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def put_space(self, name: str, text: str) -> Path:
        path = self.state / "agents" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path


class ParseTests(unittest.TestCase):
    def test_claude_code_s_shape_plus_runtime_reads_whole(self) -> None:
        found, problems = personas.parse(GOOD)
        self.assertEqual(problems, [])
        self.assertEqual((found.name, found.description, found.model, found.effort),
                         ("reviewer", "Reviews a diff and says what breaks.", "some-model", "high"))
        self.assertEqual(found.tools, ("read", "search", "shell:git diff"))
        self.assertEqual((found.disallowed_tools, found.mcp_servers), (("web_fetch",), ("github",)))
        self.assertEqual(found.body, "Read the diff. Say what breaks, with the line.")
        self.assertEqual(found.version, version_of(GOOD.encode()))

    def test_absent_lists_mean_no_narrowing(self) -> None:
        found, _ = personas.parse(agent("a"))
        self.assertEqual((found.tools, found.disallowed_tools, found.mcp_servers, found.runtime), (None, (), None, None))

    def test_each_mistake_is_reported_with_its_line(self) -> None:
        text = ("---\nname: Bad Name\ndescription: x\ntools: read, Bash\neffort: huge\npermissionMode: plan\n"
                "hooks: {}\ncolour: red\nruntime: no-such-runtime\n---\nbody\n")
        found, problems = personas.parse(text)
        self.assertIsNotNone(found)
        got = {p.key: p.line for p in problems}
        self.assertEqual(got, {"name": 2, "tools": 4, "effort": 5, "permissionMode": 6, "hooks": 7, "colour": 8,
                               "runtime": 9})
        self.assertIn("Bash", next(p.message for p in problems if p.key == "tools"))
        self.assertEqual(found.problems, tuple(problems))

    def test_required_keys(self) -> None:
        _, problems = personas.parse("---\ntools: read\n---\n")
        self.assertEqual({p.key for p in problems}, {"name", "description"})

    def test_what_isnt_an_agent_file_has_no_agent(self) -> None:
        for text in ("just text", "---\nname: a\n", "---\nname: [a\n---\n", "---\n- a\n---\n", ""):
            with self.subTest(text=text):
                found, problems = personas.parse(text)
                self.assertIsNone(found)
                self.assertEqual(len(problems), 1)

    def test_an_installed_runtime_is_accepted(self) -> None:
        from services.cowork_agent.registry.agent_registry import all_agents

        name = all_agents()[0].name
        _, problems = personas.parse(agent("a", extra=f"runtime: {name}\n"))
        self.assertEqual(problems, [])

    def test_may_is_json_ready_and_reads_as_coordinators_do(self) -> None:
        import json

        from services.cowork_agent.storm.presets.coordinators import READ_SET

        self.assertEqual(set(personas.MAY), {"read", "edit", "run"})
        self.assertEqual(personas.MAY["read"]["tools"], list(READ_SET))
        json.dumps(personas.MAY)


class FolderTests(_Case):
    def test_rel_path_per_level(self) -> None:
        self.assertEqual(personas.rel_path("project", "a"), ".xo/agents/a.md")
        self.assertEqual(personas.rel_path("space", "a"), "a.md")
        with self.assertRaises(ValueError):
            personas.rel_path("other", "a")

    def test_listing_marks_which_agent_replaces_which(self) -> None:
        self.put_project("reviewer", GOOD)
        self.put_project("mine", agent("mine"))
        self.put_space("reviewer", agent("reviewer", "the Space's"))
        self.put_space("helper", agent("helper"))
        self.put_space(".hidden", agent("hidden"))
        (self.state / "agents" / "notes.txt").write_text("not an agent")
        got = [(p.level, p.name, p.shadows, p.shadowed) for p in personas.listing("demo")]
        self.assertEqual(got, [
            ("project", "mine", None, None),
            ("project", "reviewer", {"name": "reviewer", "level": "space"}, None),
            ("space", "helper", None, None),
            ("space", "reviewer", None, {"name": "reviewer", "level": "project"}),
        ])
        first = personas.listing("demo")[1]
        self.assertEqual((first.path, first.version), (".xo/agents/reviewer.md", version_of(GOOD.encode())))
        self.assertEqual([p.name for p in personas.listing(None)], ["helper", "reviewer"])

    def test_listing_without_folders_is_empty_and_a_missing_project_is_refused(self) -> None:
        self.assertEqual(personas.listing("demo"), [])
        self.assertFalse((self.state / "agents").exists(), "listing makes no folder")
        with self.assertRaises(FileRefused) as caught:
            personas.listing("nope")
        self.assertEqual(caught.exception.code, "project_not_found")

    def test_a_broken_file_is_listed_with_its_problems(self) -> None:
        self.put_project("broken", "no front matter")
        self.put_project("other", agent("different"))
        broken, other = personas.listing("demo")
        self.assertEqual((broken.name, broken.level, len(broken.problems)), ("broken", "project", 1))
        self.assertEqual(other.name, "other")
        self.assertIn("differs from the file's name", other.problems[-1].message)

    def test_resolve_takes_the_winner_or_the_level_asked(self) -> None:
        self.put_project("reviewer", GOOD)
        self.put_space("reviewer", agent("reviewer", "the Space's"))
        self.put_space("helper", agent("helper"))
        self.assertEqual(personas.resolve("demo", "reviewer", None).level, "project")
        self.assertEqual(personas.resolve("demo", "reviewer", "space").description, "the Space's")
        self.assertEqual(personas.resolve("demo", "helper").level, "space")
        self.assertIsNone(personas.resolve("demo", "helper", "project"))
        self.assertIsNone(personas.resolve("demo", "absent"))
        self.assertIsNone(personas.resolve("gone", "reviewer", "project"))
        self.assertEqual(personas.resolve("gone", "helper").level, "space")
        for bad in ("../x", "a/b", "UP", ""):
            with self.subTest(bad=bad):
                self.assertIsNone(personas.resolve("demo", bad))

    def test_a_link_is_never_followed(self) -> None:
        outside = self.base / "outside.md"
        outside.write_text(agent("sneaky"))
        (self.project / ".xo" / "agents").mkdir(parents=True)
        (self.project / ".xo" / "agents" / "sneaky.md").symlink_to(outside)
        found = personas.resolve("demo", "sneaky", "project")
        self.assertTrue(found.problems)
        self.assertEqual(found.description, "")


class ProjectFilesTests(_Case):
    """The project-files code edits both folders, and nothing else of ``.xo/`` changes place."""

    def test_the_project_s_agents_are_the_one_editable_place_in_xo(self) -> None:
        for rel in (".xo/agents", ".xo/agents/a.md", ".XO/Agents/a.md", ".xo/agents/sub/a.md"):
            with self.subTest(rel=rel):
                self.assertEqual(classify(rel), Place.ORDINARY)
                self.assertFalse(is_noise(rel))
        for rel in (".xo/project.json", ".xo/agentsx/a.md", ".xo/agents.md", ".xo"):
            with self.subTest(rel=rel):
                self.assertEqual(classify(rel), Place.READ_ONLY)
                self.assertTrue(is_noise(rel))
        self.assertEqual(classify(".xo/agents/.git/config"), Place.RUNS_CODE)

    def test_a_project_agent_is_made_saved_and_versioned(self) -> None:
        ops.mkdir("demo", ".xo/agents")
        made = save.save("demo", personas.rel_path("project", "reviewer"), GOOD, None)
        found = personas.resolve("demo", "reviewer")
        self.assertEqual(found.version, made.version)
        save.save("demo", found.path, GOOD.replace("high", "low"), found.version)
        self.assertEqual(personas.resolve("demo", "reviewer").effort, "low")
        listed = edits.listing("demo", self.project.resolve(), path=".xo/agents/reviewer.md")
        self.assertEqual(len(listed), 1, "merged into one version within the window")

    def test_the_space_folder_is_a_project_under_its_reserved_id(self) -> None:
        rel = personas.rel_path("space", "helper")
        made = save.save(SPACE_ID, rel, agent("helper"), None)
        self.assertEqual((self.state / "agents" / "helper.md").read_text(), agent("helper"))
        self.assertEqual(read.read_text(SPACE_ID, rel).version, made.version)
        self.assertEqual(personas.resolve("demo", "helper").version, made.version)
        page = asyncio.run(versions.listing(SPACE_ID, rel))
        self.assertEqual((len(page["versions"]), page["head"]), (1, None))
        self.assertTrue(edits.state_dir(SPACE_ID).is_relative_to(self.state / "projects" / SPACE_ID))
        done = ops.delete(SPACE_ID, rel)
        self.assertFalse((self.state / "agents" / "helper.md").exists())
        self.assertFalse(any(p.name == ".space" for p in (self.state / "agents").iterdir()))
        ops.undo(SPACE_ID, done.edit_id)
        self.assertTrue((self.state / "agents" / "helper.md").exists())
        with self.assertRaises(FileRefused):
            locate(SPACE_ID, "../projects/x")

    def test_a_project_called_space_is_still_that_project(self) -> None:
        (self.projects / "space").mkdir()
        (self.projects / "space" / "a.txt").write_text("real\n")
        self.assertEqual(read.read_text("space", "a.txt").content, "real\n")
        with self.assertRaises(FileRefused):
            read.read_text(SPACE_ID, "a.txt")

    def test_the_edit_log_reads_a_project_s_agents(self) -> None:
        from datetime import datetime, timedelta, timezone

        start = datetime.now(timezone.utc) - timedelta(seconds=1)
        ops.mkdir("demo", ".xo/agents")
        save.save("demo", ".xo/agents/a.md", agent("a"), None)
        self.assertEqual([e["path"] for e in edits.edits_since(self.project, start)], [".xo/agents/a.md"])


class FileRouteTests(_Case):
    """``GET /api/xo-projects/{id}/file`` reads the Space's agents, and every other id as before."""

    def get(self, project_id: str, rel: str, **kw):
        from routers.cowork_agent.bff.xo_projects import project_file

        return asyncio.run(project_file(project_id, rel, commit=kw.get("commit"), commit_path=None,
                                        purpose="edit", version=None))

    def test_the_space_id_reads_its_agents_and_has_no_commits(self) -> None:
        self.put_space("helper", agent("helper"))
        with mock.patch("routers.cowork_agent.bff.xo_projects.touch"):
            got = self.get(SPACE_ID, "helper.md")
            self.assertEqual(got.content, agent("helper"))
            with self.assertRaises(HTTPException) as caught:
                self.get(SPACE_ID, "helper.md", commit="HEAD")
        self.assertEqual(caught.exception.status_code, 404)

    def test_a_project_called_space_still_resolves_as_before(self) -> None:
        (self.projects / "space").mkdir()
        (self.projects / "space" / "a.txt").write_text("real\n")
        with mock.patch("routers.cowork_agent.bff.xo_projects.touch"):
            self.assertEqual(self.get("space", "a.txt").content, "real\n")
            with self.assertRaises(HTTPException) as caught:
                self.get("nope", "a.txt")
        self.assertEqual(caught.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
