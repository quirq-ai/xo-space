"""The projects module: what ``modules/projects/`` declares and answers.

``tests/test_modules.py`` holds every module to the general contract; this
file pins the projects-specific facts:

* the manifest declares api, commands and the two pages, and lists the
  legacy route prefixes it keeps serving as aliases;
* the routes answer the same paths the BFF routers did (the list, a
  project's records, the rollup, the Space timeline, the graph views) and
  the old import paths resolve to the moved objects;
* ``projects.workitem_changed`` fires through ``services.signals`` when a
  workitem is created, claimed, released, closed or assigned;
* the List page spec validates, reads a route, and its read answers every
  field the spec names; the Graph page spec validates and names a widget
  file that exports the three functions;
* ``events.TYPES`` covers every workitem action the stores emit, and the
  commands answer through the facade.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from modules.projects import commands, events, routes, service, store
from modules.projects.events import WORKITEM_CHANGED
from services import modules as registry
from services import signals
from services.cowork_agent.visualizer.ingest.events import WORKITEM_ACTIONS
from services.errors import ServiceError
from tests.support import ROOT, SAMPLE_PROJECT, Sandbox, client

PAGES = ROOT / "modules" / "projects" / "pages"
PAGE_SCHEMA = ROOT / "services" / "schema" / "page.schema.json"
TIMELINE_SCHEMA = ROOT / "services" / "cowork_agent" / "visualizer" / "schema" / "timeline.schema.json"
WIDGET = ROOT / "modules" / "projects" / "ui" / "graph.js"


class _ProjectsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox(self)
        registry.reset_for_tests()
        self.addCleanup(registry.reset_for_tests)
        self.client = client(routes.router)


class ManifestTests(_ProjectsCase):
    def test_the_manifest_declares_what_the_folder_implements(self) -> None:
        module = registry.get("projects")
        self.assertEqual(module.folder, None, "the module writes into shared tiers only")
        self.assertTrue(module.declares("api"))
        self.assertTrue(module.declares("commands"))
        self.assertTrue(module.declares("pages"))
        self.assertFalse(module.declares("tasks"))
        self.assertEqual(sorted(p.id for p in registry.pages("projects")), ["graph", "list"])
        for alias in ("/api/xo-projects", "/api/workspace/workitems", "/xo/space.json", "/xo/dashboard.json"):
            self.assertIn(alias, module.aliases)

    def test_every_route_sits_under_the_aliases(self) -> None:
        module = registry.get("projects")
        allowed = ("/api/projects",) + module.aliases
        for route in routes.router.routes:
            path = getattr(route, "path", "")
            self.assertTrue(any(path == a or path.startswith(a + "/") for a in allowed), path)

    def test_the_old_import_paths_resolve_to_the_moved_objects(self) -> None:
        from routers.cowork_agent.bff import project_management as old_management
        from routers.cowork_agent.bff import xo_projects as old_projects
        from services import project_management as old_service_management
        from services import xo_structure as old_structure
        from services.cowork_agent import scopes
        from services.cowork_agent.visualizer import todos_store as old_todos

        import modules.projects.project_management as new_management
        import modules.projects.todos_store as new_todos
        import modules.projects.xo_structure as new_structure

        self.assertIs(old_projects, routes)
        self.assertIs(old_management, routes)
        self.assertIs(old_service_management, new_management)
        self.assertIs(old_structure, new_structure)
        self.assertIs(old_todos, new_todos)
        self.assertIs(scopes.VisualizerScope, service.VisualizerScope)
        self.assertIs(scopes.WorkspaceVisualizerScope, service.WorkspaceVisualizerScope)
        self.assertIsInstance(scopes.resolve_scope("xo-projects-visualizer", SAMPLE_PROJECT), service.VisualizerScope)
        self.assertIsInstance(scopes.resolve_scope("secrets"), scopes.SecretsScope)
        with self.assertRaises(service.ScopeNotFound):
            service.resolve_scope("secrets")

    def test_the_declared_files_cover_the_records(self) -> None:
        committed = {f.pattern for f in store.FILES if f.tier == "committed"}
        self.assertEqual(committed, {"project.json", "todos.json", "workitems.json", "peers.json"})
        for name in committed:
            self.assertTrue((ROOT / "tests" / "fixtures" / "xo-project" / ".xo" / name).is_file(), name)
        local = {f.pattern for f in store.FILES if f.tier == "local"}
        self.assertEqual(local, {"projects/<pid>/github/issues.json", "projects/<pid>/workitems/claims.json"})
        self.assertEqual({f.role for f in store.FILES if f.pattern.endswith("issues.json")}, {"fact"})


class RouteTests(_ProjectsCase):
    def test_the_list_and_the_records_answer_at_the_old_paths(self) -> None:
        listing = self.client.get("/api/xo-projects")
        self.assertEqual(listing.status_code, 200, listing.text)
        ids = [row["id"] for row in listing.json()["items"]]
        self.assertIn(SAMPLE_PROJECT, ids)
        self.assertEqual(listing.json()["total"], len(ids))
        base = f"/api/xo-projects/{SAMPLE_PROJECT}"
        for path in ("/todos", "/workitems", "/peers", "/activity", "/timeline", "/tree", "/removal"):
            with self.subTest(path=path):
                response = self.client.get(base + path)
                self.assertEqual(response.status_code, 200, response.text)
        (self.sandbox.project / "NOTES.md").write_text("# Notes\n\nA sample note.\n", encoding="utf-8")
        preview = self.client.get(base + "/file", params={"relative_path": "NOTES.md"})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["kind"], "markdown")
        history = self.client.get(base + "/file-history", params={"relative_path": "NOTES.md"})
        self.assertEqual(history.status_code, 200, history.text)
        self.assertFalse(history.json()["is_repo"])
        self.assertEqual(self.client.get("/api/xo-projects/timeline").status_code, 200)
        rollup = self.client.get("/api/workspace/workitems")
        self.assertEqual(rollup.status_code, 200, rollup.text)
        self.assertIn("workitems", rollup.json())

    def test_the_typed_failures_keep_their_codes(self) -> None:
        missing = self.client.get("/api/xo-projects/no-such-project/todos")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"]["code"], "project_not_found")
        tree = self.client.get(f"/api/xo-projects/{SAMPLE_PROJECT}/tree", params={"relative_path": "../"})
        self.assertEqual(tree.status_code, 400)
        self.assertEqual(tree.json()["detail"]["code"], "invalid_relative_path")
        preview = self.client.get(f"/api/xo-projects/{SAMPLE_PROJECT}/file", params={"relative_path": "photo.png"})
        self.assertEqual(preview.status_code, 415)
        self.assertEqual(preview.json()["detail"]["code"], "preview_unsupported")
        bad = self.client.get(f"/api/xo-projects/{SAMPLE_PROJECT}/timeline", params={"before": "yesterday"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["detail"]["code"], "invalid_query")

    def test_the_graph_views_serve_the_facade_payload_without_caching(self) -> None:
        payload = {"root": {"id": "xo", "label": "XO"}, "hubs": [], "groups": [], "leaves": [], "ties": [],
                   "categories": {}, "meta": {}}
        with patch.object(service, "view_payload", new=AsyncMock(return_value=payload)) as view:
            space = self.client.get("/xo/space.json")
            dashboard = self.client.get("/xo/dashboard.json")
        self.assertEqual(space.status_code, 200, space.text)
        self.assertEqual(space.json(), payload)
        self.assertEqual(space.headers["cache-control"], "no-store")
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual([c.args[0] for c in view.await_args_list], ["space", "dashboard"])

    def test_the_graph_view_answers_503_when_nothing_can_be_served(self) -> None:
        from services.cowork_agent.visualizer.workspace import views as workspace_views

        with patch.object(workspace_views, "read", return_value=(None, None)), \
             patch.object(workspace_views, "build", side_effect=RuntimeError("walk exploded")):
            response = self.client.get("/xo/space.json")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "space_unavailable")

    def test_clone_and_removal_refuse_a_browser_from_another_origin(self) -> None:
        refused = self.client.post("/api/xo-projects", json={"project_id": "new", "repository_url": "https://example.com/org/new.git"},
                                   headers={"Origin": "https://evil.example"})
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertEqual(refused.json()["detail"]["code"], "same_origin_required")
        self.assertFalse((self.sandbox.projects / "new").exists())
        cross = self.client.request("DELETE", f"/api/xo-projects/{SAMPLE_PROJECT}",
                                    json={"confirm_project_id": SAMPLE_PROJECT},
                                    headers={"Origin": "https://evil.example"})
        self.assertEqual(cross.status_code, 403)
        self.assertEqual(cross.json()["detail"]["code"], "same_origin_required")
        self.assertTrue(self.sandbox.project.is_dir(), "nothing was removed")


class SignalTests(_ProjectsCase):
    def setUp(self) -> None:
        super().setUp()
        self.seen: list[dict] = []
        listener = lambda **payload: self.seen.append(payload)  # noqa: E731
        signals.on(WORKITEM_CHANGED, listener)
        self.addCleanup(signals.off, WORKITEM_CHANGED, listener)

    def test_the_signal_is_declared(self) -> None:
        self.assertEqual(events.SIGNALS, ("workitem_changed",))
        self.assertIn(WORKITEM_CHANGED, registry.signals_declared())

    def test_workitem_transitions_raise_the_signal_with_a_toolkit_free_payload(self) -> None:
        base = f"/api/xo-projects/{SAMPLE_PROJECT}/workitems"
        created = self.client.post(base, json={"runtime": "test", "title": "Ship it"})
        self.assertEqual(created.status_code, 201, created.text)
        wid = created.json()["id"]
        self.assertEqual(self.client.post(f"{base}/{wid}/claim", json={"session_id": "s1", "runtime": "test"}).status_code, 200)
        self.assertEqual(self.client.delete(f"{base}/{wid}/claim").status_code, 200)
        assigned = self.client.put(f"{base}/{wid}/assignee", json={"assignee": "@sam"})
        self.assertEqual(assigned.status_code, 200, assigned.text)
        closed = self.client.patch(f"{base}/{wid}", json={"status": "closed"})
        self.assertEqual(closed.status_code, 200, closed.text)
        actions = [p["action"] for p in self.seen]
        self.assertEqual(actions, ["created", "claimed", "released", "assigned", "closed"])
        for payload in self.seen:
            self.assertEqual(set(payload), {"project_id", "workitem_id", "action"})
            self.assertEqual((payload["project_id"], payload["workitem_id"]), (SAMPLE_PROJECT, wid))

    def test_a_reopen_or_a_title_edit_is_not_a_change_the_signal_reports(self) -> None:
        base = f"/api/xo-projects/{SAMPLE_PROJECT}/workitems"
        wid = self.client.post(base, json={"runtime": "test", "title": "Quiet"}).json()["id"]
        self.seen.clear()
        self.assertEqual(self.client.patch(f"{base}/{wid}", json={"title": "Quieter"}).status_code, 200)
        self.assertEqual(self.seen, [])

    def test_a_failing_listener_never_fails_the_write(self) -> None:
        def broken(**payload) -> None:
            raise RuntimeError("listener exploded")

        signals.on(WORKITEM_CHANGED, broken)
        self.addCleanup(signals.off, WORKITEM_CHANGED, broken)
        created = self.client.post(f"/api/xo-projects/{SAMPLE_PROJECT}/workitems", json={"runtime": "test", "title": "Still written"})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual([p["action"] for p in self.seen], ["created"])


class EventVocabularyTests(unittest.TestCase):
    def test_every_workitem_action_the_stores_emit_is_a_declared_type(self) -> None:
        for action in WORKITEM_ACTIONS:
            self.assertIn(f"workitem.{action}", events.TYPES)
        self.assertTrue(events.WORKITEM_CHANGE_ACTIONS <= WORKITEM_ACTIONS)

    def test_the_legacy_enum_minus_sessions_is_declared_here(self) -> None:
        schema = json.loads(TIMELINE_SCHEMA.read_text(encoding="utf-8"))
        enum = set(schema["properties"]["type"]["enum"])
        sessions = {t for t in enum if t.startswith("session.")}
        self.assertEqual(sessions, {"session.started", "session.closed"})
        self.assertEqual(enum - sessions, set(events.TYPES))
        registry.reset_for_tests()
        self.assertEqual(registry.event_types().get("workitem.created"), "projects")
        self.assertEqual(registry.event_types().get("todo.added"), "projects")
        self.assertEqual(registry.event_types().get("session.started"), "sessions")


class PageTests(_ProjectsCase):
    def _spec(self, name: str) -> dict:
        return json.loads((PAGES / f"{name}.json").read_text(encoding="utf-8"))

    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is not installed")
    def test_both_specs_validate_against_the_page_schema(self) -> None:
        import jsonschema

        schema = json.loads(PAGE_SCHEMA.read_text(encoding="utf-8"))
        tabs = {t["id"] for t in registry.tabs()}
        for name in ("list", "graph"):
            with self.subTest(page=name):
                spec = self._spec(name)
                jsonschema.Draft7Validator(schema).validate(spec)
                self.assertEqual(spec["id"], name)
                self.assertEqual(spec["tab"], "projects")
                self.assertIn(spec["tab"], tabs)

    def test_the_list_page_reads_a_route_and_its_read_answers_every_field_it_names(self) -> None:
        spec = self._spec("list")
        self.assertEqual(spec["route"], "projects/data/list")
        served = {(m, getattr(r, "path", "")) for r in routes.router.routes for m in getattr(r, "methods", ())}
        self.assertIn(("GET", spec["read"]), served)
        payload = self.client.get(spec["read"]).json()
        list_block = next(b for b in spec["blocks"] if b["type"] == "list")
        stats = next(b for b in spec["blocks"] if b["type"] == "stats")
        roots = {list_block["items"].split("|", 1)[0]} | {i["value"].split("|", 1)[0] for i in stats["items"]}
        for root in roots:
            self.assertIn(root, payload, f"the page reads {root!r}, which the payload lacks")
        rows = payload[list_block["items"]]
        self.assertTrue(rows)
        row_spec = list_block["row"]
        fields = {row_spec["title"], row_spec["detail"], row_spec["badge"]["value"], row_spec["tone"]["value"]}
        fields |= {m.split("|", 1)[0] for m in row_spec["meta"]}
        fields |= {i["value"].split("|where:", 1)[1].split("|", 1)[0].lstrip("!") for i in stats["items"] if "|where:" in i["value"]}
        self.assertEqual(list_block["key"], "id")
        for row in rows:
            for field in fields | {"id"}:
                self.assertIn(field, row, f"row lacks {field!r}")
        for action in list_block["actions"]:
            self.assertIn("open", action)
            self.assertNotIn("call", action)

    def test_the_graph_page_hands_the_whole_payload_to_the_widget(self) -> None:
        spec = self._spec("graph")
        self.assertEqual(spec["route"], "projects/data/graph")
        self.assertEqual(spec["read"], "/xo/space.json")
        served = {getattr(r, "path", "") for r in routes.router.routes}
        self.assertIn(spec["read"], served)
        [block] = spec["blocks"]
        self.assertEqual((block["type"], block["widget"], block["data"]), ("widget", "graph", ""))
        source = WIDGET.read_text(encoding="utf-8")
        for name in ("mount", "update", "destroy"):
            self.assertRegex(source, rf"export (async )?function {name}\(")
        self.assertFalse(any(line.startswith("import ") for line in source.splitlines()),
                         "the widget imports nothing: the shell feeds it through ctx")
        self.assertIn("space:preview-file", source)

    def test_the_ui_lists_both_pages_while_the_module_is_on(self) -> None:
        pages = {(p["module"], p["id"]): p for p in registry.ui()["pages"]}
        self.assertIn(("projects", "list"), pages)
        self.assertIn(("projects", "graph"), pages)
        self.assertEqual(pages[("projects", "graph")]["aliases"], ["graph", "projects/graph", "projects/files/graph"],
                         "every route the legacy atlas answered for the graph lands on the spec page")
        registry.override("projects", {"pages": {"graph": False}})
        after = {(p["module"], p["id"]) for p in registry.ui()["pages"]}
        self.assertNotIn(("projects", "graph"), after)
        self.assertIn(("projects", "list"), after)


class CommandTests(_ProjectsCase):
    def test_the_commands_answer_through_the_facade(self) -> None:
        self.assertEqual(sorted(commands.COMMANDS), ["list", "todos", "workitems"])
        listing = commands.COMMANDS["list"]([])
        self.assertIn(SAMPLE_PROJECT, [row["id"] for row in listing["items"]])
        todos = commands.COMMANDS["todos"]([SAMPLE_PROJECT])
        self.assertEqual(todos["project_id"], SAMPLE_PROJECT)
        self.assertIsInstance(todos["sessions"], dict)
        items = commands.COMMANDS["workitems"]([SAMPLE_PROJECT])
        self.assertEqual(items["count"], len(items["workitems"]))
        for name in ("todos", "workitems"):
            with self.assertRaises(ServiceError) as caught:
                commands.COMMANDS[name]([])
            self.assertEqual(caught.exception.code, "missing_project_id")
        self.assertEqual(registry.commands()["projects"].keys(), commands.COMMANDS.keys())


class FacadeTests(_ProjectsCase):
    def test_the_workspace_scope_reads_the_timeline_through_the_timeline_module(self) -> None:
        from modules.timeline import service as timeline_service

        with patch.object(timeline_service, "read", return_value=[]) as read:
            service.workspace().read_timeline(limit=5)
            service.VisualizerScope(SAMPLE_PROJECT).read_timeline(limit=5)
        self.assertEqual(read.call_count, 2)

    def test_clone_and_removal_are_the_management_functions(self) -> None:
        import modules.projects.project_management as management

        self.assertIs(service.clone_project, management.clone_project)
        self.assertIs(service.remove_project, management.remove_project)
        status = asyncio.run(service.removal_status(SAMPLE_PROJECT))
        self.assertEqual(status["project_id"], SAMPLE_PROJECT)
        self.assertIn("can_remove", status)


if __name__ == "__main__":
    unittest.main()
