"""The module contract, held by tests.

A module is a folder under ``modules/`` with a ``module.json``. These tests
hold every module to the contract in ``services/modules.py``:

1. the manifest validates and names its folder; what it declares is what
   the folder implements, and nothing is implemented undeclared;
2. a ``service.py`` never imports FastAPI or ``routers``; a module reaches
   another only through ``modules.<other>.service`` or ``.events``;
3. routes live under ``/api/<name>`` (or the manifest's aliases);
   listeners name declared signals; event types are unique;
4. every declared file has an example in the sample state root and every
   sample file under a module's folder is declared;
5. the switches work live: the gate answers 404, the supervisor stops and
   starts, pages leave ``/api/ui``, and ``PUT /api/modules/{name}`` writes
   the override file and validates its body.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import APIRouter, Depends, FastAPI

from services import modules as registry
from services import signals
from services.storage.files import File
from services.supervisor import Supervisor, Task, TaskSpec
from tests.support import Sandbox, client

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / "modules"
SCHEMAS = ROOT / "services" / "schema"
FIXTURE = ROOT / "tests" / "fixtures" / "quirq-state"


def _modules() -> list[registry.Module]:
    registry.reset_for_tests()
    return registry.modules()


def _python_files(module: registry.Module) -> list[Path]:
    return sorted(p for p in module.path.rglob("*.py") if "__pycache__" not in p.parts)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


class ManifestTests(unittest.TestCase):
    def test_every_folder_with_a_manifest_loads(self) -> None:
        folders = sorted(p.name for p in MODULES.iterdir() if p.is_dir() and (p / "module.json").is_file())
        self.assertEqual(registry.names(), folders)
        self.assertIn("agent", folders)

    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is not installed")
    def test_manifests_validate_against_the_schema(self) -> None:
        import jsonschema

        schema = json.loads((SCHEMAS / "module.schema.json").read_text(encoding="utf-8"))
        for module in _modules():
            with self.subTest(module=module.name):
                jsonschema.Draft7Validator(schema).validate(module.manifest)

    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is not installed")
    def test_page_specs_validate_against_the_schema(self) -> None:
        import jsonschema

        schema = json.loads((SCHEMAS / "page.schema.json").read_text(encoding="utf-8"))
        for page in registry.pages():
            with self.subTest(module=page.module, page=page.id):
                jsonschema.Draft7Validator(schema).validate(page.spec)
                self.assertEqual(page.id, page.path.stem, "a page file is named after its id")

    def test_declared_is_implemented_and_nothing_is_undeclared(self) -> None:
        for module in _modules():
            for kind in registry.KINDS:
                with self.subTest(module=module.name, kind=kind):
                    declared = module.declares(kind)
                    implemented = registry.implements(module.name, kind)
                    self.assertEqual(declared, implemented,
                                     f"{module.name}: {kind} declared={declared} implemented={implemented}")
                    if declared:
                        self.assertIsNotNone(registry.capability(module.name, kind))

    def test_ui_json_names_tabs_every_page_uses(self) -> None:
        tab_ids = {t["id"] for t in registry.tabs()}
        self.assertTrue(tab_ids)
        for page in registry.pages():
            with self.subTest(module=page.module, page=page.id):
                self.assertIn(page.tab, tab_ids)


class ContractTests(unittest.TestCase):
    FORBIDDEN_IN_SERVICE = ("fastapi", "starlette", "routers")

    def test_service_never_imports_the_http_layer(self) -> None:
        for module in _modules():
            service = module.path / "service.py"
            if not service.is_file():
                continue
            with self.subTest(module=module.name):
                for name in _imports(service):
                    self.assertFalse(name.split(".")[0] in self.FORBIDDEN_IN_SERVICE,
                                     f"{module.name}/service.py imports {name}")

    def test_modules_reach_each_other_only_through_the_facade(self) -> None:
        allowed_tails = {"service", "events"}
        for module in _modules():
            for path in _python_files(module):
                with self.subTest(file=str(path.relative_to(ROOT))):
                    for name in _imports(path):
                        parts = name.split(".")
                        if parts[0] != "modules" or len(parts) < 2 or parts[1] == module.name:
                            continue
                        self.assertTrue(len(parts) >= 3 and parts[2] in allowed_tails,
                                        f"{path.name} imports {name}; only modules.<other>.service or .events are allowed")

    def test_routes_stay_in_their_namespace(self) -> None:
        for module, router in registry.routers():
            if "/" in module.aliases:
                continue  # the agent module carries the whole broker surface
            allowed = (f"/api/{module.name}",) + tuple(module.aliases)
            for route in router.routes:
                path = getattr(route, "path", "")
                with self.subTest(module=module.name, path=path):
                    self.assertTrue(any(path == a or path.startswith(a.rstrip("/") + "/") or path.startswith(a) for a in allowed),
                                    f"{path} is outside /api/{module.name} and the manifest's aliases")

    def test_listeners_name_declared_signals(self) -> None:
        declared = registry.signals_declared()
        for signal, pairs in registry.listeners().items():
            with self.subTest(signal=signal, modules=[m for m, _ in pairs]):
                self.assertIn(signal, declared)

    def test_event_types_are_unique_across_modules(self) -> None:
        seen: dict[str, str] = {}
        for module in _modules():
            events = module.path / "events.py"
            if not events.is_file():
                continue
            spec = importlib.util.spec_from_file_location(f"_events_{module.name}", events)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            for event_type in getattr(mod, "TYPES", ()):
                self.assertNotIn(event_type, seen, f"{event_type} declared by {seen.get(event_type)} and {module.name}")
                seen[event_type] = module.name


class FileTableTests(unittest.TestCase):
    def test_every_declared_file_has_an_example(self) -> None:
        for module_name, spec in registry.files():
            if spec.tier != "local":
                continue
            with self.subTest(module=module_name, pattern=spec.pattern):
                matches = [p for p in FIXTURE.rglob("*") if p.is_file() and spec.matches(p.relative_to(FIXTURE).as_posix())]
                self.assertTrue(matches, f"no example under tests/fixtures/quirq-state for {spec.pattern}")

    def test_every_example_in_a_module_folder_is_declared(self) -> None:
        by_folder: dict[str, list[File]] = {}
        for module_name, spec in registry.files():
            by_folder.setdefault(spec.folder, []).append(spec)
        for module in _modules():
            if not module.folder or module.folder not in by_folder:
                continue
            folder = FIXTURE / module.folder
            if not folder.is_dir():
                continue
            for path in folder.rglob("*"):
                if not path.is_file():
                    continue
                rel = path.relative_to(FIXTURE).as_posix()
                with self.subTest(file=rel):
                    self.assertTrue(any(spec.matches(rel) for spec in by_folder[module.folder]),
                                    f"{rel} matches no File in modules/{module.name}/store.py")

    def test_file_patterns_reject_traversal_and_absolute_paths(self) -> None:
        with self.assertRaises(ValueError):
            File("/etc/passwd", role="fact")
        with self.assertRaises(ValueError):
            File("a/../b", role="fact")
        with self.assertRaises(ValueError):
            File("a/b", role="thing")


class SwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox(self)
        registry.reset_for_tests()
        self.addCleanup(registry.reset_for_tests)

    def test_defaults_come_from_the_manifest(self) -> None:
        self.assertTrue(registry.enabled("connections"))
        self.assertTrue(registry.enabled("connections", "api"))
        self.assertTrue(registry.enabled("connections", "tasks", "poller"))
        self.assertFalse(registry.enabled("connections", "listeners"))
        self.assertFalse(registry.enabled("no-such-module"))
        self.assertEqual(registry.settings("connections", "tasks", "poller")["tick_s"], 30)

    def test_override_writes_the_file_and_applies_at_once(self) -> None:
        registry.override("connections", {"tasks": {"poller": {"enabled": False, "tick_s": 5}}})
        written = json.loads(registry.settings_path().read_text(encoding="utf-8"))
        self.assertEqual(written["schema"], 1)
        self.assertEqual(written["modules"]["connections"]["tasks"]["poller"], {"enabled": False, "tick_s": 5})
        self.assertFalse(registry.enabled("connections", "tasks", "poller"))
        self.assertEqual(registry.settings("connections", "tasks", "poller")["tick_s"], 5)
        self.assertTrue(registry.enabled("connections", "api"), "one switch never touches another")
        registry.override("connections", {"enabled": False})
        self.assertFalse(registry.enabled("connections", "api"))

    def test_a_hand_edit_of_the_file_applies(self) -> None:
        registry.enabled("connections")
        path = registry.settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 1, "modules": {"connections": {"enabled": False}}}), encoding="utf-8")
        self.assertFalse(registry.enabled("connections"))

    def test_override_validates_its_body(self) -> None:
        from services.errors import ServiceError

        for bad in ({"nope": True}, {"api": "yes"}, {"tasks": {"ghost": True}}, {"enabled": "off"}, ["x"]):
            with self.subTest(body=bad), self.assertRaises(ServiceError) as caught:
                registry.override("connections", bad)
            self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(ServiceError) as caught:
            registry.override("no-such-module", {"enabled": False})
        self.assertEqual(caught.exception.status, 404)

    def test_the_gate_answers_404_while_off(self) -> None:
        router = APIRouter()

        @router.get("/api/connections/ping")
        def ping() -> dict:
            return {"ok": True}

        app = FastAPI()
        app.include_router(router, dependencies=[Depends(registry.gate("connections", "api"))])
        c = client(app=app)
        self.assertEqual(c.get("/api/connections/ping").status_code, 200)
        registry.override("connections", {"api": False})
        response = c.get("/api/connections/ping")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "module_disabled")
        registry.override("connections", {"api": True})
        self.assertEqual(c.get("/api/connections/ping").status_code, 200)

    def test_the_supervisor_stops_and_starts_on_a_flip(self) -> None:
        async def scenario() -> tuple[list[str], list[str], list[str]]:
            stop = asyncio.Event()

            async def loop() -> None:
                await stop.wait()

            spec = TaskSpec("connections", Task("poller", loop))
            sup = Supervisor()
            started = await sup.start([spec])
            registry.override("connections", {"tasks": {"poller": False}})
            first = await sup.reconcile([spec])
            registry.override("connections", {"tasks": {"poller": True}})
            second = await sup.reconcile([spec])
            running = sup.running()
            await sup.stop()
            return started, first["stopped"], running

        started, stopped, running = asyncio.run(scenario())
        self.assertEqual(started, ["connections.poller"])
        self.assertEqual(stopped, ["connections.poller"])
        self.assertEqual(running, ["connections.poller"])

    def test_pages_leave_the_ui_when_off(self) -> None:
        before = {(p["module"], p["id"]) for p in registry.ui()["pages"]}
        self.assertIn(("connections", "connections"), before)
        registry.override("connections", {"pages": {"connections": False}})
        after = {(p["module"], p["id"]) for p in registry.ui()["pages"]}
        self.assertNotIn(("connections", "connections"), after)
        self.assertEqual(before - {("connections", "connections")}, after)

    def test_signals_skip_a_module_whose_listeners_are_off(self) -> None:
        seen: list[str] = []

        async def on_it(**payload) -> None:
            seen.append(payload["toolkit"])

        saved = {k: list(v) for k, v in signals._extra.items()}
        signals._extra.clear()
        try:
            # connections declares no listeners, so a listener attributed to it never runs
            with patch.object(registry, "listeners", return_value={"connections.new_events": [("connections", on_it)]}):
                self.assertEqual(asyncio.run(signals.notify("connections.new_events", toolkit="gmail")), 0)
            self.assertEqual(seen, [])
            # a runtime listener (register_new_events_listener) is not gated by any module switch
            signals.on("connections.new_events", on_it)
            self.assertEqual(asyncio.run(signals.notify("connections.new_events", toolkit="gmail")), 1)
            self.assertEqual(seen, ["gmail"])
        finally:
            signals._extra.clear()
            signals._extra.update(saved)

    def test_the_kernel_routes(self) -> None:
        from routers.kernel import router as kernel_router
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(kernel_router)
        from routers.errors import install_service_errors
        install_service_errors(app)
        c = TestClient(app, base_url="http://127.0.0.1:5002", client=("127.0.0.1", 12345))
        listing = c.get("/api/modules").json()
        self.assertEqual(listing["schema"], 1)
        names = [m["name"] for m in listing["modules"]]
        self.assertIn("connections", names)
        self.assertIn("agent", names)
        one = c.get("/api/modules/connections").json()
        self.assertTrue(one["capabilities"]["tasks"]["items"]["poller"]["enabled"])
        response = c.put("/api/modules/connections", headers={"Origin": "http://127.0.0.1:5002"},
                         json={"tasks": {"poller": False}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["capabilities"]["tasks"]["items"]["poller"]["enabled"])
        bad = c.put("/api/modules/connections", headers={"Origin": "http://127.0.0.1:5002"}, json={"ghost": 1})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["detail"]["code"], "invalid_value")
        self.assertEqual(c.get("/api/modules/nope").status_code, 404)
        ui = c.get("/api/ui").json()
        self.assertEqual([t["id"] for t in ui["tabs"]], ["projects", "agents", "inbox", "setup"])
        self.assertTrue(all("spec" in p for p in ui["pages"]))


class GeneratedDocsTests(unittest.TestCase):
    """The reference an agent reads and the sample root's file table are
    generated from the modules, never typed: a stale copy fails here."""

    def test_the_fixture_readme_file_table_is_current(self) -> None:
        import scripts.write_layout_docs as gen

        self.assertEqual(gen.current(), gen.render(),
                         "run venv/bin/python scripts/write_layout_docs.py")

    def test_the_route_reference_is_current(self) -> None:
        import scripts.write_route_docs as gen

        self.assertTrue(gen.TARGET.is_file(), "run venv/bin/python scripts/write_route_docs.py")
        self.assertEqual(gen.TARGET.read_text(encoding="utf-8"), gen.render(),
                         "run venv/bin/python scripts/write_route_docs.py")
