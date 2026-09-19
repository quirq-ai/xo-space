"""The connectors module's contract: what ``modules/connectors/`` declares
beyond the general contract (``tests/test_modules.py`` holds every module to
that one; this file pins the connectors-specific facts).

* the old dotted paths, ``services.cowork_agent.connectors...`` and
  ``routers.cowork_agent.connectors...``, resolve to the moved module
  objects, so an import or a ``patch`` through either reaches one module
  and nothing is loaded twice under the old names;
* ``routes.router`` is the eight connector routers' routes, flat, in the
  order the broker mounted them (magicpath's ``/callback`` before vercel's),
  answering exactly the paths mounted before the move, so the route parity
  core set is unchanged;
* ``module.json`` declares api, the ``mcp_gateway`` task and commands, and
  its aliases cover every path outside ``/api/connectors``;
* ``tasks.py`` runs the Composio gateway reconcile loop;
* ``service.status()`` reports every connector, one failing probe not
  hiding the others, and the status routes answer the facade;
* ``commands.py`` answers ``status``; ``store.FILES`` is empty.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.routing import APIRoute

from modules.connectors import commands, routes, service, store, tasks
from modules.connectors.composio import byo_key
from modules.connectors.composio import service as composio_service
from modules.connectors.routers import (
    composio, composio_mcp_proxy, gdrive, github_cli, github_pat, magicpath, onedrive, vercel,
)
from modules.connectors.vercel import Connection
from services import modules as registry
from services.supervisor import Task
from tests.support import Sandbox, client

ROOT = Path(__file__).resolve().parents[1]

#: Every old dotted path and the module it now names.
ALIASES = {
    "services.cowork_agent.connectors.token_store": "modules.connectors.token_store",
    "services.cowork_agent.connectors.composio": "modules.connectors.composio",
    "services.cowork_agent.connectors.composio.action_prefs": "modules.connectors.composio.action_prefs",
    "services.cowork_agent.connectors.composio.byo_key": "modules.connectors.composio.byo_key",
    "services.cowork_agent.connectors.composio.categories": "modules.connectors.composio.categories",
    "services.cowork_agent.connectors.composio.client": "modules.connectors.composio.client",
    "services.cowork_agent.connectors.composio.identity": "modules.connectors.composio.identity",
    "services.cowork_agent.connectors.composio.mcp": "modules.connectors.composio.mcp",
    "services.cowork_agent.connectors.composio.paths": "modules.connectors.composio.paths",
    "services.cowork_agent.connectors.composio.service": "modules.connectors.composio.service",
    "services.cowork_agent.connectors.composio.space_scope": "modules.connectors.composio.space_scope",
    "services.cowork_agent.connectors.gdrive": "modules.connectors.gdrive",
    "services.cowork_agent.connectors.gdrive.provider": "modules.connectors.gdrive.provider",
    "services.cowork_agent.connectors.onedrive": "modules.connectors.onedrive",
    "services.cowork_agent.connectors.onedrive.provider": "modules.connectors.onedrive.provider",
    "services.cowork_agent.connectors.github": "modules.connectors.github",
    "services.cowork_agent.connectors.github.cli_auth": "modules.connectors.github.cli_auth",
    "services.cowork_agent.connectors.github.common": "modules.connectors.github.common",
    "services.cowork_agent.connectors.github.issue_actions": "modules.connectors.github.issue_actions",
    "services.cowork_agent.connectors.github.issues": "modules.connectors.github.issues",
    "services.cowork_agent.connectors.github.pat": "modules.connectors.github.pat",
    "services.cowork_agent.connectors.vercel": "modules.connectors.vercel",
    "services.cowork_agent.connectors.vercel.api": "modules.connectors.vercel.api",
    "services.cowork_agent.connectors.vercel.connector": "modules.connectors.vercel.connector",
    "services.cowork_agent.connectors.vercel.oauth": "modules.connectors.vercel.oauth",
    "services.cowork_agent.connectors.rclone": "modules.connectors.rclone",
    "services.cowork_agent.connectors.rclone.connector": "modules.connectors.rclone.connector",
    "services.cowork_agent.connectors.rclone.oauth_lock": "modules.connectors.rclone.oauth_lock",
    "routers.cowork_agent.connectors.composio": "modules.connectors.routers.composio",
    "routers.cowork_agent.connectors.composio_mcp_proxy": "modules.connectors.routers.composio_mcp_proxy",
    "routers.cowork_agent.connectors.gdrive": "modules.connectors.routers.gdrive",
    "routers.cowork_agent.connectors.onedrive": "modules.connectors.routers.onedrive",
    "routers.cowork_agent.connectors.github_cli": "modules.connectors.routers.github_cli",
    "routers.cowork_agent.connectors.github_pat": "modules.connectors.routers.github_pat",
    "routers.cowork_agent.connectors.vercel": "modules.connectors.routers.vercel",
    "routers.cowork_agent.connectors.magicpath": "modules.connectors.routers.magicpath",
}

#: The mount order ``routers/cowork_agent/__init__.py`` kept for years: magicpath before vercel.
ROUTERS = (
    gdrive.router, onedrive.router, github_pat.router, github_cli.router,
    magicpath.router, vercel.router, composio.router, composio_mcp_proxy.router,
)

#: Every path the module answers, by first appearance in mount order: the
#: surface the broker mounted before the move, each in the parity core set.
PATHS = [
    "/api/connectors/gdrive/remotes",
    "/api/connectors/gdrive/sessions/{session_id}",
    "/api/connectors/gdrive/remotes/{name}",
    "/api/connectors/gdrive/sessions/{session_id}/cancel",
    "/api/connectors/gdrive/sessions/{session_id}/submit",
    "/api/connectors/gdrive/remotes/{name}/mkdir",
    "/api/connectors/gdrive/remotes/{name}/folders",
    "/api/connectors/gdrive/remotes/{name}/rmdir",
    "/api/connectors/gdrive/remotes/{name}/upload",
    "/api/connectors/onedrive/remotes",
    "/api/connectors/onedrive/sessions/{session_id}",
    "/api/connectors/onedrive/remotes/{name}",
    "/api/connectors/onedrive/sessions/{session_id}/cancel",
    "/api/connectors/onedrive/sessions/{session_id}/submit",
    "/api/connectors/github/token",
    "/api/connectors/github/status",
    "/api/connectors/github/disconnect",
    "/api/connectors/github/reconnect",
    "/api/connectors/github/cli/start",
    "/api/connectors/github/cli/poll",
    "/api/connectors/github/cli/cancel",
    "/api/connectors/magicpath/status",
    "/api/connectors/magicpath/setup",
    "/api/connectors/magicpath/login",
    "/api/connectors/magicpath/logout",
    "/callback",
    "/api/connectors/vercel/token",
    "/api/connectors/vercel/status",
    "/api/connectors/vercel/reconnect",
    "/api/connectors/vercel/disconnect",
    "/api/connectors/vercel/oauth/start",
    "/api/connectors/vercel/oauth/exchange",
    "/.well-known/oauth-protected-resource",
    "/api/connectors/composio/backend",
    "/api/connectors/composio/api-key",
    "/api/connectors/composio/toolkits",
    "/api/connectors/composio/{toolkit}/connect",
    "/api/connectors/composio/{toolkit}/status",
    "/api/connectors/composio/{toolkit}/disconnect",
    "/api/connectors/composio/{toolkit}/accounts/{connected_account_id}/unlink",
    "/api/connectors/composio/{toolkit}/scope",
    "/api/connectors/composio/{toolkit}/accounts",
    "/api/connectors/composio/{toolkit}/accounts/{connected_account_id}/alias",
    "/api/connectors/composio/{toolkit}/tools",
    "/api/connectors/composio/{toolkit}/prefs",
    "/api/connectors/composio/callback",
    "/mcp/cowork-proxy",
    "/mcp/cowork-proxy/",
    "/mcp/composio-proxy",
    "/mcp/composio-proxy/",
    "/mcp/cowork-proxy/u/{token}",
    "/mcp/cowork-proxy/u/{token}/",
    "/mcp/composio-proxy/u/{token}",
    "/mcp/composio-proxy/u/{token}/",
]


class AliasTests(unittest.TestCase):
    def test_every_old_path_is_the_moved_module(self) -> None:
        for old, new in ALIASES.items():
            with self.subTest(old=old):
                self.assertIs(importlib.import_module(old), importlib.import_module(new))

    def test_the_old_packages_re_export_the_same_names(self) -> None:
        from services.cowork_agent.connectors import token_store as old_store
        from services.cowork_agent.connectors.gdrive import list_drive_remotes
        from services.cowork_agent.connectors.github import get_github_token
        from modules.connectors import gdrive as gdrive_pkg, github as github_pkg, token_store

        self.assertIs(old_store, token_store)
        self.assertIs(list_drive_remotes, gdrive_pkg.list_drive_remotes)
        self.assertIs(get_github_token, github_pkg.get_github_token)

    def test_a_patch_through_the_old_path_reaches_the_moved_module(self) -> None:
        with patch("services.cowork_agent.connectors.composio.byo_key.configured", return_value=True):
            self.assertTrue(byo_key.configured())
        sentinel = object()
        with patch("routers.cowork_agent.connectors.composio.composio_service", sentinel):
            self.assertIs(composio.composio_service, sentinel)
        self.assertIs(composio.composio_service, composio_service)

    def test_nothing_is_loaded_under_the_old_names(self) -> None:
        for old in ALIASES:
            importlib.import_module(old)
        prefixes = ("services.cowork_agent.connectors.", "routers.cowork_agent.connectors.")
        stale = sorted(name for name, mod in sys.modules.items()
                       if name.startswith(prefixes) and getattr(mod, "__name__", "") == name)
        self.assertEqual(stale, [], "a module was loaded a second time under its old name")


class RouteTests(unittest.TestCase):
    def test_the_router_is_the_eight_routers_flat_in_mount_order(self) -> None:
        def rows(route_list):
            return [(r.path, tuple(sorted(r.methods or ())), r.endpoint) for r in route_list]

        self.assertTrue(all(isinstance(r, APIRoute) for r in routes.router.routes))
        self.assertEqual(rows(routes.router.routes), [row for r in ROUTERS for row in rows(r.routes)])
        self.assertEqual(routes.ROUTERS, ROUTERS)

    def test_the_paths_are_the_ones_the_broker_mounted(self) -> None:
        seen: list[str] = []
        for route in routes.router.routes:
            if route.path not in seen:
                seen.append(route.path)
        self.assertEqual(seen, PATHS)

    def test_magicpaths_callback_comes_before_vercels(self) -> None:
        callbacks = [r.endpoint.__module__ for r in routes.router.routes if r.path == "/callback"]
        self.assertEqual(callbacks, ["modules.connectors.routers.magicpath", "modules.connectors.routers.vercel"])
        out_of_schema = [r.include_in_schema for r in routes.router.routes if r.path == "/callback"]
        self.assertEqual(out_of_schema, [False, True], "magicpath's dispatcher stays out of the schema")

    def test_every_path_sits_under_the_manifest_aliases(self) -> None:
        module = registry.get("connectors")
        allowed = ("/api/connectors",) + tuple(module.aliases)
        for path in PATHS:
            with self.subTest(path=path):
                self.assertTrue(any(path == a or path.startswith(a.rstrip("/") + "/") for a in allowed), path)


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox(self)
        registry.reset_for_tests()
        self.addCleanup(registry.reset_for_tests)

    def test_the_manifest_declares_what_the_folder_implements(self) -> None:
        module = registry.get("connectors")
        self.assertIsNone(module.folder)
        for kind in ("api", "tasks", "commands"):
            self.assertTrue(module.declares(kind), kind)
            self.assertTrue(registry.implements("connectors", kind), kind)
        for kind in ("stream", "listeners", "pages"):
            self.assertFalse(module.declares(kind), kind)
            self.assertFalse(registry.implements("connectors", kind), kind)
        self.assertEqual(set(module.aliases), {"/api/connectors", "/mcp", "/callback", "/.well-known"})
        self.assertTrue(registry.enabled("connectors"))
        self.assertTrue(registry.enabled("connectors", "api"))
        self.assertTrue(registry.enabled("connectors", "tasks", "mcp_gateway"))
        self.assertIs(registry.capability("connectors", "api").router, routes.router)

    def test_the_gateway_task_is_declared(self) -> None:
        self.assertEqual([t.name for t in tasks.TASKS], ["mcp_gateway"])
        task = tasks.TASKS[0]
        self.assertIsInstance(task, Task)
        self.assertIsNone(task.enabled)
        self.assertFalse(task.oneshot)
        self.assertTrue(task.description)
        self.assertIn("connectors.mcp_gateway", [spec.key for spec in registry.tasks()])
        self.assertTrue(registry.describe_one("connectors")["capabilities"]["tasks"]["items"]["mcp_gateway"]["enabled"])

    def test_the_task_runs_the_composio_reconcile_loop(self) -> None:
        with patch.object(composio_service, "gateway_reconcile_loop", new=AsyncMock()) as loop:
            asyncio.run(tasks.TASKS[0].start())
        loop.assert_awaited_once_with()

    def test_the_module_writes_no_file_under_the_state_root(self) -> None:
        self.assertEqual(store.FILES, [])
        self.assertEqual([m for m, _ in registry.files() if m == "connectors"], [])
        self.assertFalse((ROOT / "tests" / "fixtures" / "quirq-state" / "connectors").exists())


class StatusTests(unittest.TestCase):
    """The facade over a sandbox with no Composio key and every network probe stubbed."""

    def setUp(self) -> None:
        self.sandbox = Sandbox.fresh(self)
        os.environ.pop(byo_key.ENV_VAR, None)
        self.addCleanup(lambda: os.environ.pop(byo_key.ENV_VAR, None))
        key_path = patch.object(byo_key, "_KEY_PATH", self.sandbox.base / "composio" / "api_key.json")
        key_path.start()
        self.addCleanup(key_path.stop)

    def _quiet(self, **overrides):
        stubs = {
            "github_status": AsyncMock(return_value={"status": "needs_auth"}),
            "vercel_status": AsyncMock(return_value=Connection(status="needs_auth")),
            "rclone_available": AsyncMock(return_value=False),
            "magicpath_status": AsyncMock(return_value={"cli_installed": False, "logged_in": False}),
        }
        stubs.update(overrides)
        patchers = [patch.object(service, name, new=stub) for name, stub in stubs.items()]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_status_reports_every_connector_in_order(self) -> None:
        self._quiet()
        report = asyncio.run(service.status())
        self.assertEqual(list(report), ["connectors"])
        self.assertEqual(list(report["connectors"]), list(service.CONNECTORS))
        self.assertEqual(report["connectors"]["composio"], {"mode": "inactive", "key_source": None})
        self.assertEqual(report["connectors"]["github"], {"status": "needs_auth"})
        self.assertEqual(report["connectors"]["vercel"], {"status": "needs_auth"})
        self.assertEqual(report["connectors"]["gdrive"], {"available": False, "remotes": []})
        self.assertEqual(report["connectors"]["onedrive"], {"available": False, "remotes": []})
        self.assertEqual(report["connectors"]["magicpath"], {"cli_installed": False, "logged_in": False})

    def test_one_failing_probe_does_not_hide_the_others(self) -> None:
        async def boom() -> dict:
            raise RuntimeError("no network")

        self._quiet(github_status=boom)
        with self.assertLogs("modules.connectors.service", "WARNING"):
            report = asyncio.run(service.status())
        self.assertEqual(report["connectors"]["github"], {"status": "error", "error": "no network"})
        self.assertEqual(report["connectors"]["composio"]["mode"], "inactive")
        self.assertEqual(report["connectors"]["vercel"], {"status": "needs_auth"})

    def test_the_rclone_stores_list_remotes_when_the_daemon_answers(self) -> None:
        with patch.object(service, "rclone_available", new=AsyncMock(return_value=True)), \
                patch.object(service.gdrive, "list_drive_remotes", new=AsyncMock(return_value=[{"name": "drive"}])), \
                patch.object(service.onedrive, "list_onedrive_remotes", new=AsyncMock(return_value=[])):
            self.assertEqual(asyncio.run(service.gdrive_status()), {"available": True, "remotes": [{"name": "drive"}]})
            self.assertEqual(asyncio.run(service.onedrive_status()), {"available": True, "remotes": []})

    def test_the_status_routes_answer_the_facade(self) -> None:
        c = client(routes.router)
        with patch.object(service, "github_status", new=AsyncMock(return_value={"status": "connected", "username": "octo"})):
            self.assertEqual(c.get("/api/connectors/github/status").json(), {"status": "connected", "username": "octo"})
        with patch.object(service, "vercel_status", new=AsyncMock(return_value=Connection(status="failed", error="revoked"))):
            response = c.get("/api/connectors/vercel/status")
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.json(), {"status": "failed", "error": "revoked"})
        with patch.object(service, "rclone_available", new=AsyncMock(return_value=False)):
            self.assertEqual(c.get("/api/connectors/gdrive/remotes").status_code, 503)
            self.assertEqual(c.get("/api/connectors/onedrive/remotes").status_code, 503)
        with patch.object(service, "rclone_available", new=AsyncMock(return_value=True)), \
                patch.object(service.onedrive, "list_onedrive_remotes", new=AsyncMock(return_value=[{"name": "od"}])):
            self.assertEqual(c.get("/api/connectors/onedrive/remotes").json(), {"remotes": [{"name": "od"}]})
        self.assertEqual(c.get("/api/connectors/composio/backend").json(), {"mode": "inactive", "key_source": None})
        with patch.object(magicpath, "probe_status", new=AsyncMock(return_value={"cli_installed": False})):
            self.assertEqual(c.get("/api/connectors/magicpath/status").json(), {"cli_installed": False})
            self.assertEqual(asyncio.run(service.magicpath_status()), {"cli_installed": False})

    def test_the_status_command(self) -> None:
        self.assertEqual(set(commands.COMMANDS), {"status"})
        registry.reset_for_tests()
        self.addCleanup(registry.reset_for_tests)
        self.assertEqual(registry.commands()["connectors"], commands.COMMANDS)
        with patch.object(service, "status", new=AsyncMock(return_value={"connectors": {}})):
            self.assertEqual(asyncio.run(commands.COMMANDS["status"]([])), {"connectors": {}})


if __name__ == "__main__":
    unittest.main()
