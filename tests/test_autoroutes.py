"""routers/autoroutes.py: folder-based routes generated from config.

Builds a throwaway package tree in a temp dir, points the loader's repo root
at it, and checks the generated table and behaviour through a TestClient:
paths from folder + module + function, GET for zero-parameter functions,
a typed JSON body otherwise, ``__all__`` and underscore filtering, the
subtree switch, the ServiceError mapping and the browser guard.
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import autoroutes

PKG = "autoroutes_fixture"


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


class AutoroutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _write(self.root, f"{PKG}/__init__.py", """
            def ping():
                return {"ok": True}
        """)
        _write(self.root, f"{PKG}/items/__init__.py", "")
        _write(self.root, f"{PKG}/items/service.py", """
            from __future__ import annotations
            from services.errors import ServiceError

            __all__ = ["list_items", "create_item", "fail", "sleepy"]

            def list_items(status: str = "open", limit: int = 5) -> dict:
                \"\"\"Items by status.\"\"\"
                return {"status": status, "limit": limit}

            def create_item(title: str, tags: list[str] | None = None):
                return {"title": title, "tags": tags or []}

            def fail():
                raise ServiceError("nope", "not today", status=409)

            async def sleepy(n: int):
                return n * 2

            def hidden_by_all():
                return "no"

            def _private():
                return "no"
        """)
        _write(self.root, f"{PKG}/items/_helpers.py", """
            def helper():
                return "no"
        """)
        _write(self.root, f"{PKG}/off/__init__.py", """
            def nothing():
                return "no"
        """)
        _write(self.root, f"{PKG}/varargs.py", """
            def anything(*args, **kwargs):
                return "no"
        """)
        sys.path.insert(0, str(self.root))
        self._root_patch = patch.object(autoroutes, "REPO_ROOT", self.root)
        self._root_patch.start()

    def tearDown(self) -> None:
        self._root_patch.stop()
        sys.path.remove(str(self.root))
        for name in [m for m in sys.modules if m == PKG or m.startswith(PKG + ".")]:
            del sys.modules[name]
        self.tmp.cleanup()

    def _client(self, config: dict) -> tuple[TestClient, list[dict]]:
        router, table = autoroutes.build_router(config)
        app = FastAPI()
        app.include_router(router)
        return TestClient(app), table

    def test_nothing_is_exposed_without_config(self) -> None:
        _, table = autoroutes.build_router({"folders": {}})
        self.assertEqual(table, [])

    def test_table_maps_folder_module_function_to_path(self) -> None:
        _, table = self._client({"guard": False, "folders": {PKG: True, f"{PKG}/off": False}})
        routes = {row["path"]: row["method"] for row in table}
        self.assertEqual(routes, {
            f"/{PKG}/ping": "GET",
            f"/{PKG}/items/service/list_items": "POST",
            f"/{PKG}/items/service/create_item": "POST",
            f"/{PKG}/items/service/fail": "GET",
            f"/{PKG}/items/service/sleepy": "POST",
        })
        # __all__, underscores, the off subtree and *args functions are all out.
        self.assertNotIn(f"/{PKG}/items/service/hidden_by_all", routes)
        self.assertNotIn(f"/{PKG}/items/_helpers/helper", routes)
        self.assertNotIn(f"/{PKG}/off/nothing", routes)
        self.assertNotIn(f"/{PKG}/varargs/anything", routes)

    def test_child_can_be_enabled_under_a_disabled_parent(self) -> None:
        _, table = self._client({"guard": False, "folders": {f"{PKG}/items": True}})
        self.assertEqual(
            sorted(row["path"] for row in table),
            sorted([
                f"/{PKG}/items/service/list_items",
                f"/{PKG}/items/service/create_item",
                f"/{PKG}/items/service/fail",
                f"/{PKG}/items/service/sleepy",
            ]),
        )

    def test_calls_go_through_typed_bodies(self) -> None:
        client, _ = self._client({"guard": False, "folders": {PKG: True}})
        self.assertEqual(client.get(f"/{PKG}/ping").json(), {"ok": True})
        r = client.post(f"/{PKG}/items/service/list_items", json={})
        self.assertEqual(r.json(), {"status": "open", "limit": 5})
        r = client.post(f"/{PKG}/items/service/list_items", json={"limit": "many"})
        self.assertEqual(r.status_code, 422)
        r = client.post(f"/{PKG}/items/service/create_item", json={"title": "t", "tags": ["a"]})
        self.assertEqual(r.json(), {"title": "t", "tags": ["a"]})
        r = client.post(f"/{PKG}/items/service/create_item", json={})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(client.post(f"/{PKG}/items/service/sleepy", json={"n": 4}).json(), 8)

    def test_service_error_becomes_code_and_message(self) -> None:
        client, _ = self._client({"guard": False, "folders": {PKG: True}})
        r = client.get(f"/{PKG}/items/service/fail")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json(), {"detail": {"code": "nope", "message": "not today"}})

    def test_guard_refuses_a_foreign_origin(self) -> None:
        client, _ = self._client({"guard": True, "folders": {PKG: True}})
        # TestClient connects from "testclient", not a loopback peer.
        r = client.get(f"/{PKG}/ping", headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"]["code"], "local_only")
        with patch.object(autoroutes, "is_local_mutation", return_value=True):
            self.assertEqual(client.get(f"/{PKG}/ping").json(), {"ok": True})

    def test_docstring_first_line_is_the_summary(self) -> None:
        client, _ = self._client({"guard": False, "folders": {PKG: True}})
        spec = client.get("/openapi.json").json()
        op = spec["paths"][f"/{PKG}/items/service/list_items"]["post"]
        self.assertEqual(op["summary"], "Items by status.")
        self.assertEqual(op["tags"], [PKG])


class ConfigTests(unittest.TestCase):
    def test_missing_file_exposes_nothing(self) -> None:
        cfg = autoroutes.load_config(Path("/nonexistent/autoroutes.json"))
        self.assertEqual(cfg, {"guard": True, "folders": {}})

    def test_repo_default_exposes_nothing(self) -> None:
        cfg = autoroutes.load_config()
        self.assertEqual(cfg["folders"], {})
        self.assertTrue(cfg["guard"])

    def test_malformed_file_is_a_loud_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "autoroutes.json"
            path.write_text("[1, 2]", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                autoroutes.load_config(path)

    def test_longest_configured_ancestor_wins(self) -> None:
        folders = {"a": True, "a/b": False, "a/b/c": True}
        self.assertTrue(autoroutes._enabled("a/x", folders))
        self.assertFalse(autoroutes._enabled("a/b", folders))
        self.assertFalse(autoroutes._enabled("a/b/d", folders))
        self.assertTrue(autoroutes._enabled("a/b/c/deep", folders))
        self.assertFalse(autoroutes._enabled("z", folders))


if __name__ == "__main__":
    unittest.main()
