"""The opt-in, credential lifecycle, and real SDK HTTP protocol in an isolated Space."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.browser_guard import add_forwarding_middleware
from routers.mcp_server import router
from services.errors import ServiceError
from services.mcp_server import service, store
from services import access_tokens


class MCPStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_default_off_without_writing(self):
        self.assertFalse(store.read()["enabled"])
        self.assertFalse(store.config_path().exists())
        with self.assertRaises(ServiceError) as failure:
            store.authenticate("Bearer anything")
        self.assertEqual(failure.exception.status, 404)

    def test_only_hash_persisted_enable_rotate_disable_and_reenable(self):
        data, token = store.update(enabled=True)
        self.assertTrue(data["enabled"])
        self.assertGreaterEqual(len(token), 40)
        saved = store.config_path().read_text()
        self.assertNotIn(token, saved)
        self.assertEqual(stat.S_IMODE(store.config_path().stat().st_mode), 0o600)
        store.authenticate(f"Bearer {token}")
        self.assertIsNone(store.update(enabled=True)[1])
        _, rotated = store.update(rotate=True)
        with self.assertRaises(ServiceError):
            store.authenticate(f"Bearer {token}")
        store.authenticate(f"bearer {rotated}")
        store.update(enabled=False)
        self.assertIsNone(store.read()["token_hash"])
        with self.assertRaises(ServiceError):
            store.authenticate(f"Bearer {rotated}")
        _, fresh = store.update(enabled=True)
        self.assertNotIn(fresh, (token, rotated))

    def test_corrupt_and_future_settings_fail_closed_and_are_not_overwritten(self):
        path = store.config_path()
        path.parent.mkdir(parents=True)
        for content in ('{', '[]', '{"schema": 2, "enabled": true}',
                        '{"schema": 1, "enabled": "false"}',
                        '{"schema": 1, "enabled": true, "token_hash": null}'):
            with self.subTest(content=content):
                path.write_text(content)
                with self.assertRaises(ServiceError):
                    store.authenticate("Bearer token")
                with self.assertRaises(ServiceError):
                    store.update(enabled=True)
                self.assertEqual(path.read_text(), content)

    def test_write_failure_is_actionable(self):
        with patch.object(access_tokens, "write_json_atomic", side_effect=OSError("secret/path")):
            with self.assertRaises(ServiceError) as failure:
                store.update(enabled=True)
        self.assertEqual(failure.exception.status, 503)
        self.assertNotIn("secret/path", str(failure.exception))


class MCPHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.projects = Path(self.tmp.name) / "projects"
        self.projects.mkdir()
        env = patch.dict(os.environ, {
            "QUIRQ_STATE_ROOT": str(Path(self.tmp.name) / "state"),
            "XO_PROJECTS_ROOT": str(self.projects),
        })
        env.start()
        self.addCleanup(env.stop)
        self.app = FastAPI(lifespan=service.lifespan)
        add_forwarding_middleware(self.app)
        self.app.include_router(router)
        self.client = self.enterContext(TestClient(
            self.app, base_url="http://localhost:5002", client=("127.0.0.1", 49152),
        ))

    def enable(self):
        response = self.client.put("/api/mcp-server", json={"enabled": True})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def rpc(self, method, *, token, params=None, request_id=1, headers=None):
        return self.client.post("/mcp", json={
            "jsonrpc": "2.0", "id": request_id, "method": method,
            **({"params": params} if params is not None else {}),
        }, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-03-26",
            **(headers or {}),
        })

    def test_status_validation_and_one_time_token(self):
        status = self.client.get("/api/mcp-server")
        self.assertFalse(status.json()["enabled"])
        self.assertEqual(status.headers["cache-control"], "no-store")
        for body in ({}, {"enabled": "true"}, {"enabled": True, "port": 123}):
            self.assertEqual(self.client.put("/api/mcp-server", json=body).status_code, 422)
        self.assertEqual(self.client.post("/api/mcp-server/rotate-token").status_code, 409)
        token = self.enable()
        status = self.client.get("/api/mcp-server")
        self.assertTrue(status.json()["token_configured"])
        self.assertNotIn(token, status.text)
        self.assertNotIn("token_hash", status.text)
        again = self.client.put("/api/mcp-server", json={"enabled": True})
        self.assertNotIn("token", again.json())

    def test_legacy_handshake_tool_list_call_and_revocation(self):
        token = self.enable()
        response = self.rpc("initialize", token=token, params={
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "space-test", "version": "1.0"},
        })
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertEqual(result["serverInfo"]["name"], "space")
        self.assertIn("tools", result["capabilities"])
        self.assertNotIn("mcp-session-id", response.headers)
        initialized = self.client.post("/mcp", json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }, headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"})
        self.assertEqual(initialized.status_code, 202, initialized.text)
        listing = self.rpc("tools/list", token=token)
        self.assertEqual(listing.status_code, 200, listing.text)
        tools = listing.json()["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {
            "space_list_projects", "space_read_project_document", "space_list_todos", "space_list_inbox",
        })
        self.assertTrue(all(tool["annotations"]["readOnlyHint"] for tool in tools))
        called = self.rpc("tools/call", token=token, params={"name": "space_list_projects", "arguments": {}})
        self.assertEqual(called.status_code, 200, called.text)
        self.assertFalse(called.json()["result"].get("isError", False), called.text)
        self.assertEqual(called.headers["cache-control"], "no-store")
        bad_tool = self.rpc("tools/call", token=token, params={"name": "unknown", "arguments": {}})
        self.assertTrue(bad_tool.json()["result"].get("isError") or bad_tool.json().get("error"), bad_tool.text)
        rotated = self.client.post("/api/mcp-server/rotate-token").json()["token"]
        self.assertEqual(self.rpc("tools/list", token=token).status_code, 401)
        self.assertEqual(self.rpc("tools/list", token=rotated).status_code, 200)
        self.client.put("/api/mcp-server", json={"enabled": False})
        self.assertEqual(self.rpc("tools/list", token=rotated).status_code, 404)

    def test_every_transport_method_is_gated(self):
        for method in ("GET", "POST", "DELETE"):
            self.assertEqual(self.client.request(method, "/mcp").status_code, 404)
        self.enable()
        for method in ("GET", "POST", "DELETE"):
            response = self.client.request(method, "/mcp")
            self.assertEqual(response.status_code, 401)
            self.assertIn("Bearer", response.headers["www-authenticate"])

    def test_modern_clients_discover_and_call_tools_without_a_handshake(self):
        token = self.enable()
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        for method, params in (
            ("tools/list", {"_meta": meta}),
            ("tools/call", {"_meta": meta, "name": "space_list_projects", "arguments": {}}),
        ):
            response = self.rpc(method, token=token, params=params,
                                headers={"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method,
                                         **({"Mcp-Name": params["name"]} if "name" in params else {})})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("result", response.json(), response.text)
            self.assertFalse(response.json()["result"].get("isError"), response.text)

    def test_protocol_rejects_bad_json_and_oversized_bodies(self):
        token = self.enable()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json", "MCP-Protocol-Version": "2025-03-26"}
        response = self.client.post("/mcp", content="{", headers=headers)
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/mcp", content=" " * (64 * 1024 + 1), headers=headers)
        self.assertEqual(response.status_code, 413)

    def test_cross_site_and_rebinding_settings_and_transport_are_denied(self):
        token = self.enable()
        for headers in (
            {"Origin": "https://other.example"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Origin": "http://evil.example", "Host": "evil.example"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.client.get("/api/mcp-server", headers=headers).status_code, 403)
                self.assertEqual(self.client.put("/api/mcp-server", json={"enabled": False}, headers=headers).status_code, 403)
                self.assertEqual(self.rpc("tools/list", token=token, headers=headers).status_code, 403)

    def test_remote_client_uses_token_but_cannot_manage_settings(self):
        token = self.enable()
        with TestClient(self.app, base_url="https://space.example", client=("198.51.100.2", 123)) as client:
            self.assertEqual(client.get("/api/mcp-server").status_code, 403)
            self.assertEqual(client.post("/api/mcp-server/rotate-token").status_code, 403)
            response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={
                "Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-03-26",
            })
            self.assertEqual(response.status_code, 200, response.text)


if __name__ == "__main__":
    unittest.main()
