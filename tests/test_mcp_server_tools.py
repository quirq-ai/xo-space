from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from services.cowork_agent import project_layout
from services.inbox import store as inbox_store
from services.mcp_server import tools


class MCPToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.projects = self.base / "projects"
        self.state = self.base / "state"
        environment = patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(self.projects), "QUIRQ_STATE_ROOT": str(self.state),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.server = MCPServer("Space tool tests")
        tools.register_tools(self.server)

    def write_json(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def project(self, name: str = "demo", **metadata) -> Path:
        path = self.projects / name
        self.write_json(path / ".xo" / "project.json", {"name": name, **metadata})
        return path

    async def call(self, name: str, **arguments) -> dict:
        result = await self.server.call_tool(name, arguments)
        self.assertFalse(result.is_error)
        return json.loads(result.content[0].text)

    async def test_catalog_describes_read_only_tools_and_bounded_inputs(self) -> None:
        catalog = {tool.name: tool for tool in await self.server.list_tools()}
        self.assertEqual(set(catalog), {tool["name"] for tool in tools.TOOL_DESCRIPTIONS})
        for tool in catalog.values():
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertFalse(tool.annotations.destructive_hint)
            self.assertFalse(tool.annotations.open_world_hint)
        limit = catalog["space_list_projects"].input_schema["properties"]["limit"]
        self.assertEqual((limit["minimum"], limit["maximum"], limit["default"]), (1, 100, 50))
        document = catalog["space_read_project_document"].input_schema["properties"]["document"]
        self.assertIn("README.md", document["enum"])
        self.assertNotIn(".env", document["enum"])

    async def test_empty_reads_do_not_create_state_or_projects(self) -> None:
        result = await self.call("space_list_projects")
        self.assertEqual(result["projects"], [])
        result = await self.call("space_list_inbox")
        self.assertEqual(result["items"], [])
        self.assertEqual(list(self.base.iterdir()), [])

    def test_project_root_default_still_creates_directory(self) -> None:
        self.assertFalse(project_layout.xo_projects_root(create=False).exists())
        self.assertTrue(project_layout.xo_projects_root().is_dir())

    async def test_project_pagination_and_metadata_allowlist(self) -> None:
        self.project("alpha", display_name="Alpha", description="First", token="secret", path="/private/path")
        beta = self.project("beta")
        self.write_json(beta / ".xo" / "project.json", {"name": "old-name", "display_name": "old-name"})
        self.project(".hidden")
        result = await self.call("space_list_projects", limit=1, offset=0)
        self.assertEqual(result["projects"], [{
            "project_id": "alpha", "display_name": "Alpha", "description": "First",
        }])
        self.assertEqual(result["total"], 2)
        self.assertTrue(result["has_more"])
        result = await self.call("space_list_projects", limit=1, offset=1)
        self.assertEqual(result["projects"][0]["display_name"], "beta")
        self.assertFalse(result["has_more"])

    async def test_rejects_invalid_limits_and_filters(self) -> None:
        for arguments in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": True}):
            with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                await self.call("space_list_projects", **arguments)
        with self.assertRaises(ToolError):
            await self.call("space_list_inbox", status="deleted")

    async def test_rejects_project_traversal_and_hidden_directories(self) -> None:
        for project_id in ("../demo", "/tmp/demo", "demo/../other", "demo\\other", ".secret", "demo\x00"):
            with self.subTest(project_id=project_id), self.assertRaises(ToolError):
                await self.call("space_read_project_document", project_id=project_id, document="README.md")

    async def test_read_document_is_bounded_and_preserves_utf8(self) -> None:
        project = self.project()
        (project / "README.md").write_text("A project document", encoding="utf-8")
        result = await self.call("space_read_project_document", project_id="demo", document="README.md")
        self.assertEqual(result["content"], "A project document")
        self.assertFalse(result["truncated"])
        content = "a" * (tools.DOCUMENT_BYTES - 1) + "🙂extra"
        (project / "README.md").write_text(content, encoding="utf-8")
        result = await self.call("space_read_project_document", project_id="demo", document="README.md")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["content"], "a" * (tools.DOCUMENT_BYTES - 1))

    async def test_document_enum_rejects_other_files(self) -> None:
        self.project()
        for document in (".env", "../README.md", ".xo/project.json", "/etc/passwd"):
            with self.subTest(document=document), self.assertRaises(ToolError):
                await self.call("space_read_project_document", project_id="demo", document=document)

    async def test_project_and_metadata_symlinks_are_not_exposed(self) -> None:
        outside = self.base / "outside"
        self.write_json(outside / ".xo" / "project.json", {"name": "external", "description": "secret"})
        self.projects.mkdir()
        (self.projects / "external").symlink_to(outside, target_is_directory=True)
        project = self.project("linked-metadata")
        (project / ".xo" / "project.json").unlink()
        (project / ".xo" / "project.json").symlink_to(outside / ".xo" / "project.json")
        result = await self.call("space_list_projects")
        self.assertEqual(result["projects"], [])
        for project_id in ("external", "linked-metadata"):
            with self.subTest(project_id=project_id), self.assertRaises(ToolError):
                await self.call("space_list_todos", project_id=project_id)

    async def test_document_and_todo_symlinks_are_rejected(self) -> None:
        project = self.project()
        secret = project / ".env"
        secret.write_text("TOKEN=do-not-expose", encoding="utf-8")
        (project / "README.md").symlink_to(secret)
        (project / ".xo" / "todos.json").symlink_to(secret)
        for name, arguments in (
            ("space_read_project_document", {"document": "README.md"}),
            ("space_list_todos", {}),
        ):
            with self.subTest(name=name), self.assertRaises(ToolError) as error:
                await self.call(name, project_id="demo", **arguments)
            self.assertNotIn(str(self.base), str(error.exception))

    async def test_todos_omit_deleted_items_unknown_fields_and_source_paths(self) -> None:
        project = self.project()
        self.write_json(project / ".xo" / "todos.json", {"sessions": {
            "session": {"source_file": "/private/session.json", "todos": [
                {"id": "one", "content": "First", "status": "pending", "token": "secret"},
                {"id": "deleted", "content": "Gone", "deleted_at": "2026-09-01"},
                {"id": "two", "content": "Second", "status": "done"},
            ]},
        }})
        result = await self.call("space_list_todos", project_id="demo", limit=1)
        self.assertEqual(result["total"], 2)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["todos"][0]["id"], "one")
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("source_file", json.dumps(result))
        self.assertNotIn("/private", json.dumps(result))

    async def test_inbox_filters_without_refreshing_or_migrating(self) -> None:
        self.write_json(inbox_store.inbox_path(), {"items": [
            {"id": "00000001", "title": "First", "status": "new", "ts": "2026-09-01T00:00:00Z", "token": "secret"},
            {"id": "00000002", "title": "Second", "status": "seen", "ts": "2026-09-02T00:00:00Z"},
            {"id": "00000003", "title": "Done", "status": "done", "ts": "2026-09-03T00:00:00Z"},
        ]})
        legacy = self.projects / ".xo" / "inbox.json"
        self.write_json(legacy, {"items": []})
        before = {str(path.relative_to(self.base)): path.read_bytes() for path in self.base.rglob("*") if path.is_file()}
        with patch.object(inbox_store, "_adopt_legacy", side_effect=AssertionError("must not migrate")):
            result = await self.call("space_list_inbox", limit=1)
            self.assertEqual(result["items"][0]["title"], "Second")
            self.assertEqual(result["counts"], {"new": 1, "seen": 1, "done": 1})
            self.assertEqual(result["total"], 2)
            self.assertTrue(result["has_more"])
            result = await self.call("space_list_inbox", status="done")
            self.assertEqual([item["title"] for item in result["items"]], ["Done"])
            result = await self.call("space_list_inbox", status="all")
            self.assertEqual(result["total"], 3)
            self.assertNotIn("secret", json.dumps(result))
        after = {str(path.relative_to(self.base)): path.read_bytes() for path in self.base.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((self.state / ".locks").exists())

    async def test_inbox_symlink_and_corrupt_file_fail_without_exposing_paths(self) -> None:
        path = inbox_store.inbox_path()
        path.parent.mkdir(parents=True)
        path.write_text("{broken-json", encoding="utf-8")
        with self.assertRaises(ToolError) as error:
            await self.call("space_list_inbox")
        self.assertNotIn(str(self.base), str(error.exception))
        path.unlink()
        secret = self.base / "secrets.json"
        secret.write_text("{}", encoding="utf-8")
        path.symlink_to(secret)
        with self.assertRaises(ToolError):
            await self.call("space_list_inbox")

    async def test_io_errors_do_not_disclose_filesystem_paths(self) -> None:
        with patch.object(tools, "_list_projects", side_effect=PermissionError("cannot open /private/user")):
            with self.assertRaises(ToolError) as error:
                await self.call("space_list_projects")
        self.assertNotIn("/private/user", str(error.exception))
        self.assertIn("permissions", str(error.exception))

    async def test_sync_reads_run_off_the_event_loop(self) -> None:
        loop_thread = threading.get_ident()
        def read_in_thread(limit: int, offset: int) -> dict:
            return {"thread": threading.get_ident()}
        with patch.object(tools, "_list_projects", side_effect=read_in_thread):
            result = await self.call("space_list_projects")
        self.assertNotEqual(result["thread"], loop_thread)


if __name__ == "__main__":
    unittest.main()
