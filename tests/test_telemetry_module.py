"""The telemetry module: what ``modules/telemetry/`` declares and answers.

``tests/test_modules.py`` holds every module to the general contract; this
file pins the telemetry-specific facts:

* the manifest declares api, the two tasks, commands and the Configure
  page, owns ``usage/`` and lists the legacy prefixes it keeps serving;
* the routes answer the same paths the usage routers, the telemetry
  sources router and ``/xo/sessions.json`` did, with the same bodies and
  the same failure shapes, and the old import paths resolve to the moved
  objects while the old routers carry no routes;
* the tasks are declared with their gates (``QUIRQ_WATCHER_ENABLED`` beside
  the switch) and stand by while the agent module's copy runs;
* every declared file has an example in the sample state root and the
  ``usage/`` examples are all declared;
* the Configure page spec validates, reads a served route and names only
  fields that route answers; the commands answer through the facade.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import APIRouter, FastAPI

from modules.telemetry import commands, routes, service, store, tasks
from services import modules as registry
from services.errors import ServiceError
from services.storage.files import File
from services.supervisor import Supervisor, TaskSpec
from tests.support import ROOT, SAMPLE_PROJECT, STATE_FIXTURE, Sandbox, client

PAGES = ROOT / "modules" / "telemetry" / "pages"
PAGE_SCHEMA = ROOT / "services" / "schema" / "page.schema.json"
SAMPLE_PID = "00000000-0000-4000-8000-000000000000"
SAMPLE_SESSION = "11111111-1111-4111-8111-111111111111"

#: Every path the module took over, and where it used to be mounted.
MOVED_PATHS = {
    # routers/cowork_agent/usage.py
    "/api/usage", "/api/usage/analytics", "/api/usage/summary", "/api/usage/summary/card",
    "/api/usage/sessions", "/api/usage/sessions/{session_id}",
    # routers/cowork_agent/bff/visualizer.py
    "/api/xo-projects/{project_id}/usage/summary/card", "/api/xo-projects/{project_id}/usage/analytics",
    "/api/xo-projects/{project_id}/usage/sessions", "/api/xo-projects/{project_id}/usage/summary",
    "/api/xo-projects/{project_id}/usage/sessions/{session_id}",
    # routers/cowork_agent/bff/workspace_visualizer.py
    "/api/xo-projects/usage", "/api/xo-projects/usage/summary/card", "/api/xo-projects/usage/analytics",
    "/api/xo-projects/usage/sessions", "/api/xo-projects/usage/summary",
    "/api/xo-projects/usage/sessions/{session_id}",
    # routers/telemetry_sources.py
    "/api/telemetry/sources", "/api/telemetry/sources/{source_id}",
    # routers/xo_data.py
    "/xo/sessions.json",
}


class FakeStore:
    """The secrets scope handle: a dict with upsert/delete semantics."""

    def __init__(self) -> None:
        self.entries: dict[str, str] = {}

    def upsert(self, key: str, value: str) -> None:
        self.entries[key] = value

    def delete(self, key: str) -> bool:
        return self.entries.pop(key, None) is not None


def _provider(source_id: str, *, config: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(SOURCE_ID=source_id, SOURCE_LABEL=source_id.title(), COST_STATUS="unavailable",
                           SOURCE_CONFIG=config)


class _TelemetryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox(self)
        registry.reset_for_tests()
        self.addCleanup(registry.reset_for_tests)
        self.client = client(routes.router)


class ManifestTests(_TelemetryCase):
    def test_the_manifest_declares_what_the_folder_implements(self) -> None:
        module = registry.get("telemetry")
        self.assertEqual(module.title, "Telemetry")
        self.assertEqual(module.folder, "usage")
        for kind in ("api", "tasks", "commands", "pages"):
            self.assertTrue(module.declares(kind), kind)
        for kind in ("stream", "listeners"):
            self.assertFalse(module.declares(kind), kind)
        self.assertEqual(sorted(module.caps["tasks"]["items"]), ["usage_sync", "watcher"])
        self.assertEqual([p.id for p in registry.pages("telemetry")], ["sources"])
        self.assertEqual(module.aliases, ("/api/usage", "/api/xo-projects", "/api/telemetry", "/xo/sessions.json"))

    def test_every_route_sits_under_the_aliases(self) -> None:
        module = registry.get("telemetry")
        allowed = ("/api/telemetry",) + module.aliases
        for route in routes.router.routes:
            path = getattr(route, "path", "")
            self.assertTrue(any(path == a or path.startswith(a + "/") for a in allowed), path)

    def test_the_routes_answer_every_path_that_moved_here(self) -> None:
        served = {getattr(r, "path", "") for r in routes.router.routes}
        self.assertEqual(served, MOVED_PATHS)
        methods = {(m, getattr(r, "path", "")) for r in routes.router.routes for m in getattr(r, "methods", ())}
        self.assertIn(("PUT", "/api/telemetry/sources/{source_id}"), methods)
        self.assertEqual({m for m, _p in methods} - {"PUT"}, {"GET"})

    def test_the_old_routers_carry_no_routes_and_the_old_paths_resolve(self) -> None:
        from routers import telemetry_sources as old_sources_router, xo_data as old_xo_data
        from routers.cowork_agent import usage as old_usage
        from routers.cowork_agent.bff import _visualizer_models as old_models
        from routers.cowork_agent.bff import _visualizer_presenter as old_presenter
        from routers.cowork_agent.bff import visualizer as old_visualizer, workspace_visualizer as old_workspace
        from routers.cowork_agent.legacy import openclaw_usage
        from services import telemetry_sources as old_sources, usage_sync as old_sync
        from services.cowork_agent.engine import usage_loader as old_loader

        import modules.telemetry.models as models
        import modules.telemetry.presenter as presenter
        import modules.telemetry.sources as sources
        import modules.telemetry.usage_loader as usage_loader
        import modules.telemetry.usage_sync as usage_sync

        for old in (old_sources_router, old_xo_data, old_usage, old_visualizer):
            self.assertEqual(old.router.routes, [], old.__name__)
        self.assertEqual([r.path for r in old_workspace.router.routes], ["/api/xo-projects/activity"])
        self.assertIs(old_sources, sources)
        self.assertIs(old_sync, usage_sync)
        self.assertIs(old_loader, usage_loader)
        self.assertIs(old_models.SessionCostSummary, models.SessionCostSummary)
        self.assertIs(old_presenter.tokens_from_stats, presenter.tokens_from_stats)
        self.assertIs(old_usage.usage_dashboard, routes.usage_dashboard)
        self.assertEqual([r.path for r in openclaw_usage.router.routes],
                         ["/openclaw/usage/analytics", "/openclaw/usage/summary", "/openclaw/usage/summary/card",
                          "/openclaw/usage/sessions", "/openclaw/usage/sessions/{session_id}"])
        self.assertIs(openclaw_usage.usage_summary, routes.usage_summary)


class UsageRouteTests(_TelemetryCase):
    def test_the_project_and_workspace_usage_read_the_sample(self) -> None:
        card = self.client.get(f"/api/xo-projects/{SAMPLE_PROJECT}/usage/summary/card")
        self.assertEqual(card.status_code, 200, card.text)
        # tokens from the sample's stats.json; the sample augment keys its row
        # by the native session id while the index merges by the composite
        # key, so no message count attaches to the row (as before the move)
        self.assertEqual((card.json()["days"], card.json()["totalTokens"], card.json()["totalMessages"]), (5, 1500, 0))
        sessions = self.client.get(f"/api/xo-projects/{SAMPLE_PROJECT}/usage/sessions")
        self.assertEqual(sessions.status_code, 200, sessions.text)
        [row] = sessions.json()["sessions"]
        self.assertEqual(row["sessionFile"], f"{SAMPLE_SESSION}.jsonl")
        summary = self.client.get(f"/api/xo-projects/{SAMPLE_PROJECT}/usage/summary")
        self.assertEqual(summary.status_code, 200, summary.text)
        self.assertEqual((summary.json()["sessionId"], summary.json()["sessionCount"]), ("all", 1))
        one = self.client.get(f"/api/xo-projects/{SAMPLE_PROJECT}/usage/sessions/{SAMPLE_SESSION}")
        self.assertEqual(one.status_code, 200, one.text)
        self.assertEqual(one.json()["durationMs"], 600000)
        analytics = self.client.get(f"/api/xo-projects/{SAMPLE_PROJECT}/usage/analytics", params={"days": 7})
        self.assertEqual(analytics.status_code, 200, analytics.text)
        self.assertEqual(len(analytics.json()["costAndTokens"]), 7)

        workspace = self.client.get("/api/xo-projects/usage", params={"days": 7})
        self.assertEqual(workspace.status_code, 200, workspace.text)
        self.assertEqual(workspace.json()["total_tokens"]["input"], 1200)
        self.assertEqual(workspace.json()["total_sessions"], 1)
        for path in ("/api/xo-projects/usage/summary/card", "/api/xo-projects/usage/analytics",
                     "/api/xo-projects/usage/sessions", "/api/xo-projects/usage/summary"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertEqual(self.client.get("/api/xo-projects/usage/summary").json()["sessionId"], "all-projects")
        found = self.client.get(f"/api/xo-projects/usage/sessions/{SAMPLE_SESSION}")
        self.assertEqual(found.status_code, 200, found.text)
        self.assertEqual(found.json()["sessionFile"], f"{SAMPLE_SESSION}.jsonl")

    def test_the_typed_failures_keep_their_codes(self) -> None:
        missing = self.client.get("/api/xo-projects/no-such-project/usage/summary")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"]["code"], "project_not_found")
        bad = self.client.get(f"/api/xo-projects/{SAMPLE_PROJECT}/usage/analytics", params={"start": "yesterday"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["detail"]["code"], "invalid_query")
        bad = self.client.get("/api/xo-projects/usage/summary", params={"end": "tomorrow"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["detail"]["code"], "invalid_query")
        for path in (f"/api/xo-projects/{SAMPLE_PROJECT}/usage/sessions/ghost", "/api/xo-projects/usage/sessions/ghost"):
            with self.subTest(path=path):
                gone = self.client.get(path)
                self.assertEqual(gone.status_code, 404)
                self.assertEqual(gone.json()["detail"]["code"], "session_not_found")

    def test_the_active_agent_usage_goes_through_the_loader(self) -> None:
        seen: list = []
        fake = SimpleNamespace(
            dashboard=lambda window: seen.append(("dashboard", window)) or {"ok": True},
            analytics=lambda window: seen.append(("analytics", window)) or {"ok": True},
            summary=lambda window: seen.append(("summary", window)) or {"ok": True},
            summary_card=lambda window: seen.append(("card", window)) or {"ok": True},
            list_sessions=lambda: [{"sessionId": "s1"}],
            get_session=lambda sid, window: {"sessionId": sid} if sid == "s1" else None,
        )
        with patch.object(service.usage_loader, "load_usage_module", return_value=fake):
            self.assertEqual(self.client.get("/api/usage", params={"days": 900, "tz": "mars"}).json(), {"ok": True})
            self.assertEqual(self.client.get("/api/usage/analytics", params={"start": "2026-01-01", "end": "2026-01-31"}).status_code, 200)
            self.assertEqual(self.client.get("/api/usage/summary").status_code, 200)
            self.assertEqual(self.client.get("/api/usage/summary/card").status_code, 200)
            self.assertEqual(self.client.get("/api/usage/sessions").json(), [{"sessionId": "s1"}])
            self.assertEqual(self.client.get("/api/usage/sessions/s1").json(), {"sessionId": "s1"})
            gone = self.client.get("/api/usage/sessions/nope")
            half = self.client.get("/api/usage/analytics", params={"start": "2026-01-01"})
        self.assertEqual(seen[0], ("dashboard", {"days": 365, "tz": "local"}))
        self.assertEqual(seen[1], ("analytics", {"start": "2026-01-01", "end": "2026-01-31", "tz": "local"}))
        self.assertEqual(seen[2], ("summary", {"days": 30, "tz": "local"}))
        self.assertEqual(seen[3], ("card", {"days": 5, "tz": "local"}))
        # the bare-message details this surface has always answered with
        self.assertEqual((gone.status_code, gone.json()["detail"]), (404, "Session nope not found"))
        self.assertEqual((half.status_code, half.json()["detail"]), (400, "start and end must be passed together"))
        with patch.object(service.usage_loader, "load_usage_module", side_effect=ModuleNotFoundError("nope")):
            unsupported = self.client.get("/api/usage")
        self.assertEqual(unsupported.status_code, 501)
        self.assertIn("no usage module for active agent", unsupported.json()["detail"])


class SourcesAndSessionsViewTests(_TelemetryCase):
    def test_the_sources_routes_list_save_and_rebuild(self) -> None:
        from modules.telemetry import sources

        provider = _provider("alpha", config={"vendor": "cursor", "path_env": "ALPHA_HOME", "path_default": "~/.alpha"})
        fake_store = FakeStore()
        with patch.object(sources, "_load_providers", return_value=[("alpha", provider)]), \
             patch.object(sources.scopes, "resolve_scope", return_value=fake_store), \
             patch.object(routes, "_rebuild_sessions_view") as rebuild, \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALPHA_HOME", None)
            os.environ.pop(sources.DISABLED_ENV, None)
            listed = self.client.get("/api/telemetry/sources")
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual(listed.json()["total"], 1)
            self.assertEqual(listed.headers["cache-control"], "no-store")
            saved = self.client.put("/api/telemetry/sources/alpha", json={"path": "/data/alpha", "enabled": False})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["item"]["path"]["configured"], "/data/alpha")
            self.assertFalse(saved.json()["item"]["enabled"])
            self.assertTrue(saved.json()["rebuilding"])
            self.client.get("/api/telemetry/sources")  # let the executor run
            self.assertEqual(fake_store.entries[sources.DISABLED_ENV], "alpha")
            bad = self.client.put("/api/telemetry/sources/alpha", json={"path": "x", "extra": 1})
            self.assertEqual(bad.status_code, 422)
            relative = self.client.put("/api/telemetry/sources/alpha", json={"path": "relative/path"})
            self.assertEqual(relative.status_code, 400)
            self.assertEqual(relative.json()["detail"]["code"], "invalid_path")
        rebuild.assert_called()
        with patch.object(sources, "_load_providers", return_value=[]):
            missing = self.client.put("/api/telemetry/sources/ghost", json={"enabled": False})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"]["code"], "unknown_source")

    def test_the_sessions_view_is_served_through_the_facade_without_caching(self) -> None:
        payload = {"schema": 1, "generated_at": "2026-01-01T00:00:00Z", "meta": {"sources": []}, "sessions": []}
        with patch.object(service, "sessions_view", new=AsyncMock(return_value=payload)):
            response = self.client.get("/xo/sessions.json")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), payload)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_the_sessions_view_rebuilds_when_stale_and_answers_503_when_nothing_can_be_served(self) -> None:
        from services.cowork_agent.visualizer.workspace import views as workspace_views

        fresh = {"schema": 1, "sessions": [], "meta": {}}
        with patch.object(workspace_views, "read", return_value=(None, None)), \
             patch.object(workspace_views, "build", return_value=fresh) as build:
            self.assertEqual(asyncio.run(service.sessions_view()), fresh)
        build.assert_called_once_with("sessions")
        stale = {"schema": 1, "sessions": [{"id": "old"}], "meta": {}}
        with patch.object(workspace_views, "read", return_value=(stale, 999999.0)), \
             patch.object(workspace_views, "build", side_effect=RuntimeError("walk exploded")):
            self.assertEqual(asyncio.run(service.sessions_view()), stale, "stale beats absent")
        with patch.object(workspace_views, "read", return_value=(None, None)), \
             patch.object(workspace_views, "build", side_effect=RuntimeError("walk exploded")):
            response = self.client.get("/xo/sessions.json")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "sessions_unavailable")
        with patch.object(workspace_views, "build", side_effect=RuntimeError("walk exploded")):
            self.assertIsNone(service.rebuild_sessions_view(), "a failed rebuild after a save never raises")


class TaskTests(unittest.TestCase):
    def test_the_tasks_are_declared_with_their_gates(self) -> None:
        self.assertEqual([t.name for t in tasks.TASKS], ["watcher", "usage_sync"])
        watcher, usage_sync = tasks.TASKS
        self.assertIs(watcher.enabled, service.watcher_enabled)
        self.assertIsNone(usage_sync.enabled)
        for task in tasks.TASKS:
            self.assertTrue(asyncio.iscoroutinefunction(task.start), task.name)
            self.assertFalse(task.oneshot, task.name)
            self.assertTrue(task.description, task.name)
        registry.reset_for_tests()
        self.addCleanup(registry.reset_for_tests)
        declared = {spec.key for spec in registry.tasks()}
        self.assertTrue({"telemetry.watcher", "telemetry.usage_sync"} <= declared)
        with patch.dict(os.environ, {"QUIRQ_WATCHER_ENABLED": "false"}):
            self.assertFalse(service.watcher_enabled())
        with patch.dict(os.environ, {"QUIRQ_WATCHER_ENABLED": "yes"}):
            self.assertTrue(service.watcher_enabled())
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QUIRQ_WATCHER_ENABLED", None)
            self.assertTrue(service.watcher_enabled(), "on by default")

class FileTableTests(unittest.TestCase):
    def test_every_declared_file_has_an_example_and_the_folder_is_covered(self) -> None:
        patterns = {f.pattern for f in store.FILES}
        self.assertEqual(patterns, {
            "usage/<agent>.json",
            "projects/<pid>/stats.json", "projects/<pid>/sessions/sessions-augment.json",
            "projects/offsets.json", "projects/<source>-offsets.json",
            "cache/stats.json", "cache/sessions/sessionslist.json", "cache/sessions/sessions-augment.json",
            "cache/heartbeat.json", "cache/activity/workspace.json", "cache/activity/projects/<project>.json",
        })
        examples = [p.relative_to(STATE_FIXTURE).as_posix() for p in STATE_FIXTURE.rglob("*") if p.is_file()]
        for spec in store.FILES:
            with self.subTest(pattern=spec.pattern):
                self.assertIsInstance(spec, File)
                self.assertEqual(spec.tier, "local")
                self.assertTrue(any(spec.matches(rel) for rel in examples), spec.pattern)
        for rel in examples:
            if rel.startswith("usage/"):
                self.assertTrue(any(spec.matches(rel) for spec in store.FILES), rel)
        self.assertEqual({f.role for f in store.FILES if f.folder == "usage"}, {"fact"})
        self.assertEqual({f.role for f in store.FILES if f.folder != "usage"}, {"cache"})
        self.assertFalse(File("projects/offsets.json", role="cache").matches("projects/sample_agent-offsets.json"))
        self.assertFalse(File("projects/<source>-offsets.json", role="cache").matches("projects/offsets.json"))

    def test_the_paths_sit_in_their_folders(self) -> None:
        sandbox = Sandbox(self)
        self.assertEqual(store.watermark_path("sample_agent"), sandbox.state / "usage" / "sample_agent.json")
        self.assertEqual(store.offsets_path(), sandbox.state / "projects" / "offsets.json")
        self.assertEqual(store.project_stats_path(SAMPLE_PID), sandbox.state / "projects" / SAMPLE_PID / "stats.json")
        self.assertEqual(store.heartbeat_path(), sandbox.state / "cache" / "heartbeat.json")
        self.assertEqual(store.workspace_stats_path(), sandbox.state / "cache" / "stats.json")
        self.assertEqual(store.workspace_sessionslist_path(), sandbox.state / "cache" / "sessions" / "sessionslist.json")
        self.assertEqual(store.workspace_activity_path(), sandbox.state / "cache" / "activity" / "workspace.json")
        self.assertEqual(store.project_activity_path(SAMPLE_PROJECT),
                         sandbox.state / "cache" / "activity" / "projects" / f"{SAMPLE_PROJECT}.json")
        for path in (store.watermark_path("sample_agent"), store.offsets_path(), store.project_stats_path(SAMPLE_PID),
                     store.project_sessions_augment_path(SAMPLE_PID), store.heartbeat_path(), store.workspace_stats_path(),
                     store.workspace_sessionslist_path(), store.workspace_sessions_augment_path(),
                     store.workspace_activity_path(), store.project_activity_path(SAMPLE_PROJECT)):
            with self.subTest(path=str(path)):
                self.assertTrue(path.is_file(), "the sample state root holds an example at every path")


class PageTests(_TelemetryCase):
    def _spec(self) -> dict:
        return json.loads((PAGES / "sources.json").read_text(encoding="utf-8"))

    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is not installed")
    def test_the_spec_validates_and_lands_on_the_agents_tab(self) -> None:
        import jsonschema

        spec = self._spec()
        jsonschema.Draft7Validator(json.loads(PAGE_SCHEMA.read_text(encoding="utf-8"))).validate(spec)
        self.assertEqual((spec["id"], spec["tab"], spec["route"], spec["label"], spec["order"]),
                         ("sources", "agents", "agents/configure", "Configure", 40))
        self.assertIn(spec["tab"], {t["id"] for t in registry.tabs()})
        pages = {(p["module"], p["id"]): p for p in registry.ui()["pages"]}
        self.assertIn(("telemetry", "sources"), pages)
        registry.override("telemetry", {"pages": {"sources": False}})
        self.assertNotIn(("telemetry", "sources"), {(p["module"], p["id"]) for p in registry.ui()["pages"]})

    def test_the_page_reads_a_served_route_and_names_only_fields_it_answers(self) -> None:
        from modules.telemetry import sources

        spec = self._spec()
        served = {(m, getattr(r, "path", "")) for r in routes.router.routes for m in getattr(r, "methods", ())}
        self.assertIn(("GET", spec["read"]), served)
        provider = _provider("alpha", config={"vendor": "cursor", "path_env": "ALPHA_HOME", "path_default": "~/.alpha"})
        with patch.object(sources, "_load_providers", return_value=[("alpha", provider), ("bare", _provider("bare"))]), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALPHA_HOME", None)
            payload = self.client.get(spec["read"]).json()
        rows = payload["items"]
        self.assertEqual(len(rows), 2)

        def has(row: dict, path: str) -> bool:
            value = row
            for part in path.split("."):
                if not isinstance(value, dict) or part not in value:
                    return False
                value = value[part]
            return True

        lists = [b for b in spec["blocks"] if b["type"] == "list"]
        self.assertEqual(len(lists), 2)
        for block in lists:
            self.assertEqual(block["items"].split("|", 1)[0], "items")
            self.assertEqual(block["key"], "id")
            row_spec = block["row"]
            fields = {row_spec["title"], row_spec["detail"], row_spec["badge"]["value"], row_spec["tone"]["value"]}
            fields |= {m for m in row_spec["meta"]}
            for row in rows:
                for field in fields | {"id"}:
                    self.assertTrue(has(row, field.split("|", 1)[0]), f"row lacks {field!r}")
            self.assertIn(("PUT", "/api/telemetry/sources/{source_id}"), served)
            expand = block["expand"]
            write = expand.get("write") or expand.get("submit")
            self.assertEqual(write, "PUT /api/telemetry/sources/{id}")
            if expand["type"] == "toggles":
                [toggle] = expand["toggles"]
                self.assertEqual((toggle["path"], toggle["value"]), ("enabled", "enabled"))
            else:
                self.assertEqual(expand["type"], "form")
                [field] = expand["fields"]
                self.assertEqual((field["name"], field["value"]), ("path", "path.configured"))
                self.assertIn("where:path.editable", block["items"])
        stats = next(b for b in spec["blocks"] if b["type"] == "stats")
        for item in stats["items"]:
            self.assertEqual(item["value"].split("|", 1)[0], "items")


class CommandTests(_TelemetryCase):
    def test_the_commands_answer_through_the_facade(self) -> None:
        from modules.telemetry import sources

        self.assertEqual(sorted(commands.COMMANDS), ["sources", "usage"])
        self.assertEqual(registry.commands()["telemetry"].keys(), commands.COMMANDS.keys())
        summary = commands.COMMANDS["usage"]([])
        self.assertEqual((summary["sessionId"], summary["sessionCount"]), ("all-projects", 1))
        self.assertEqual(commands.COMMANDS["usage"](["7"])["totalTokens"], 1500)
        with self.assertRaises(ServiceError) as caught:
            commands.COMMANDS["usage"](["soon"])
        self.assertEqual(caught.exception.code, "invalid_days")
        with patch.object(sources, "_load_providers", return_value=[("alpha", _provider("alpha"))]):
            listed = commands.COMMANDS["sources"]([])
        self.assertEqual((listed["total"], listed["items"][0]["id"]), (1, "alpha"))


class GateTests(_TelemetryCase):
    def test_the_routes_answer_404_while_the_module_is_off(self) -> None:
        app = FastAPI()
        app.include_router(routes.router, dependencies=[__import__("fastapi").Depends(registry.gate("telemetry", "api"))])
        c = client(app=app)
        with patch.object(service, "sessions_view", new=AsyncMock(return_value={"schema": 1})):
            self.assertEqual(c.get("/xo/sessions.json").status_code, 200)
            registry.override("telemetry", {"api": False})
            off = c.get("/xo/sessions.json")
        self.assertEqual(off.status_code, 404)
        self.assertEqual(off.json()["detail"]["code"], "module_disabled")


if __name__ == "__main__":
    unittest.main()
