"""Phase 2 of dynamic connectors: the catalog (list + cache + route), toolkit
detail, and custom-auth config creation + connect.

Hermetic: reuses ``test_composio_byo._KeyBase`` for the owner-only key fixture.
"""
from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.cowork_agent.connectors.composio import client as byo_client, byo_key
from services.cowork_agent.connectors.composio import catalog, service
from tests.test_composio_byo import _KeyBase


def _item(slug, name, managed=True):
    return SimpleNamespace(
        slug=slug, name=name, no_auth=False,
        composio_managed_auth_schemes=(["OAUTH2"] if managed else []),
        meta=SimpleNamespace(logo=f"https://cdn/{slug}.png", tools_count=5,
                             categories=[SimpleNamespace(id="crm", name="CRM")]))


class ListCatalogTests(_KeyBase):
    def setUp(self) -> None:
        super().setUp()
        byo_key.save("sk_live")

    def test_list_catalog_forwards_search_and_maps_items(self) -> None:
        raw = MagicMock()
        raw.toolkits.list.return_value = SimpleNamespace(
            items=[_item("gmail", "Gmail"), _item("acme", "Acme", managed=False)],
            next_cursor="c2")
        sdk = SimpleNamespace(_client=raw)
        with patch.object(byo_client, "_sdk", return_value=sdk):
            out = byo_client.list_catalog(search="gm", limit=25)
        kw = raw.toolkits.list.call_args.kwargs
        self.assertEqual(kw["search"], "gm")
        self.assertEqual(kw["limit"], 25)
        self.assertEqual(out["next_cursor"], "c2")
        self.assertEqual(out["items"][0], {
            "slug": "gmail", "name": "Gmail", "logo": "https://cdn/gmail.png",
            "categories": [{"id": "crm", "name": "CRM"}], "no_auth": False,
            "managed_auth": True, "tools_count": 5})
        self.assertFalse(out["items"][1]["managed_auth"])

    def test_toolkit_detail_extracts_creation_fields(self) -> None:
        field = SimpleNamespace(name="api_key", display_name="API Key",
                                description="Your key", type="string",
                                required=True, is_secret=True)
        detail = SimpleNamespace(
            slug="acme", name="Acme", meta=SimpleNamespace(logo="https://cdn/acme.png"),
            composio_managed_auth_schemes=[], auth_schemes=["API_KEY"],
            auth_config_detail=SimpleNamespace(fields=SimpleNamespace(
                auth_config_creation=SimpleNamespace(required=[field], optional=[]))))
        raw = MagicMock()
        raw.toolkits.retrieve.return_value = detail
        sdk = SimpleNamespace(_client=raw)
        with patch.object(byo_client, "_sdk", return_value=sdk):
            out = byo_client.toolkit_detail("acme")
        self.assertFalse(out["managed_auth"])
        self.assertEqual(out["fields"]["required"][0]["name"], "api_key")
        self.assertTrue(out["fields"]["required"][0]["is_secret"])


class CatalogCacheTests(_KeyBase):
    def setUp(self) -> None:
        super().setUp()
        byo_key.save("sk_live")
        catalog.invalidate()
        self.addCleanup(catalog.invalidate)

    def test_page_is_cached_per_query(self) -> None:
        with patch.object(catalog, "_client") as c:
            c.list_catalog.return_value = {"items": [{"slug": "gmail"}], "next_cursor": None}
            catalog.page(search="gm", limit=25)
            catalog.page(search="gm", limit=25)
            self.assertEqual(c.list_catalog.call_count, 1)      # cached
            catalog.page(search="sl", limit=25)
            self.assertEqual(c.list_catalog.call_count, 2)      # different key

    def test_featured_is_the_curated_set(self) -> None:
        self.assertEqual(set(catalog.featured()), set(service.TOOLKITS))


class CategoriesTests(_KeyBase):
    def setUp(self) -> None:
        super().setUp()
        byo_key.save("sk_live")
        catalog.invalidate()
        self.addCleanup(catalog.invalidate)

    def test_list_categories_maps_items(self) -> None:
        raw = MagicMock()
        raw.toolkits.retrieve_categories.return_value = SimpleNamespace(
            items=[SimpleNamespace(id="crm", name="CRM"),
                   SimpleNamespace(id="productivity", name="Productivity")])
        with patch.object(byo_client, "_sdk", return_value=SimpleNamespace(_client=raw)):
            out = byo_client.list_categories()
        self.assertEqual(out, [{"id": "crm", "name": "CRM"},
                               {"id": "productivity", "name": "Productivity"}])

    def test_categories_cached(self) -> None:
        with patch.object(catalog, "_client") as c:
            c.list_categories.return_value = [{"id": "crm", "name": "CRM"}]
            catalog.categories()
            catalog.categories()
            self.assertEqual(c.list_categories.call_count, 1)   # cached


class CategoriesRouteAsync(unittest.IsolatedAsyncioTestCase, _KeyBase):
    def setUp(self) -> None:
        super().setUp()
        byo_key.save("sk_live")
        catalog.invalidate()
        self.addCleanup(catalog.invalidate)

    async def test_categories_route_returns_list(self) -> None:
        from routers.cowork_agent.connectors import composio as router
        with patch.object(catalog, "categories",
                          return_value=[{"id": "crm", "name": "CRM"}]):
            resp = await router.get_categories(_req())
        self.assertEqual(json.loads(resp.body)["categories"], [{"id": "crm", "name": "CRM"}])

    async def test_categories_route_409_without_key(self) -> None:
        from fastapi import HTTPException
        from routers.cowork_agent.connectors import composio as router
        byo_key.clear()
        with self.assertRaises(HTTPException) as raised:
            await router.get_categories(_req())
        self.assertEqual(raised.exception.status_code, 409)


class CatalogRouteTests(unittest.IsolatedAsyncioTestCase, _KeyBase):
    def setUp(self) -> None:
        super().setUp()
        byo_key.save("sk_live")

    async def test_catalog_route_clamps_limit_and_returns_featured(self) -> None:
        from routers.cowork_agent.connectors import composio as router
        with patch.object(catalog, "page",
                          return_value={"items": [{"slug": "gmail"}], "next_cursor": None}) as pg, \
                patch.object(catalog, "featured", return_value=["gmail"]):
            resp = await router.get_catalog(_req(), search="gm", limit=999)
        self.assertEqual(pg.call_args.kwargs["limit"], 50)     # clamped
        body = json.loads(resp.body)
        self.assertEqual(body["featured"], ["gmail"])
        self.assertEqual(body["items"], [{"slug": "gmail"}])

    async def test_catalog_route_409_without_key(self) -> None:
        from fastapi import HTTPException
        from routers.cowork_agent.connectors import composio as router
        byo_key.clear()
        with self.assertRaises(HTTPException) as raised:
            await router.get_catalog(_req())
        self.assertEqual(raised.exception.status_code, 409)


class CustomAuthTests(_KeyBase):
    def setUp(self) -> None:
        super().setUp()
        byo_key.save("sk_live")

    def test_create_custom_auth_config_shape_and_cache(self) -> None:
        acfg = MagicMock()
        acfg.create.return_value = SimpleNamespace(id="ac_custom")
        with patch.object(byo_client, "_sdk", return_value=SimpleNamespace(auth_configs=acfg)):
            ac = byo_client.create_custom_auth_config("acme", "API_KEY", {"api_key": "sk"})
        self.assertEqual(ac, "ac_custom")
        slug, options = acfg.create.call_args.args
        self.assertEqual(slug, "acme")
        self.assertEqual(options["type"], "use_custom_auth")
        self.assertEqual(options["auth_scheme"], "API_KEY")
        self.assertEqual(options["credentials"], {"api_key": "sk"})
        self.assertEqual(byo_key.load_auth_configs()["acme"], "ac_custom")


class ConnectCustomAuthTests(_KeyBase):
    def setUp(self) -> None:
        super().setUp()
        byo_key.save("sk_live")

    def test_initiate_connection_creates_custom_config_then_connects(self) -> None:
        with patch.dict(os.environ, {"COMPOSIO_DYNAMIC_CONNECTORS": "1",
                                     "COMPOSIO_CALLBACK_URL": "https://x/cb"}), \
                patch.object(service.swarm_client, "create_custom_auth_config") as mk, \
                patch.object(service.swarm_client, "connect",
                             return_value={"auth_url": "https://a", "connection_request_id": "r"}) as conn:
            out = service.initiate_connection(
                "user_x", "acme", auth_scheme="API_KEY", credentials={"api_key": "sk"})
        mk.assert_called_once_with("acme", "API_KEY", {"api_key": "sk"})
        conn.assert_called_once()
        self.assertEqual(out["connection_request_id"], "r")

    def test_unknown_toolkit_curated_mode_still_raises(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COMPOSIO_DYNAMIC_CONNECTORS", None)
            with self.assertRaises(ValueError):
                service.initiate_connection("user_x", "nosuchtoolkit")


def _req(headers=None):
    from starlette.requests import Request
    scope = {"type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": "/", "raw_path": b"/", "query_string": b"", "server": ("127.0.0.1", 5002),
             "client": ("127.0.0.1", 1),
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    return Request(scope, receive)


if __name__ == "__main__":
    unittest.main()
