"""CLI routes reuse Space operations with an independent, revocable opt-in."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.browser_guard import add_forwarding_middleware
from routers.cli_access import router
from services import cli_access, space_tools
from services.errors import ServiceError
from services.mcp_server import store as mcp_store


class CLIAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {
            "QUIRQ_STATE_ROOT": str(self.root / "state"),
            "XO_PROJECTS_ROOT": str(self.root / "projects"),
        })
        env.start()
        self.addCleanup(env.stop)
        # No MCP lifespan is running: CLI must work independently.
        self.app = FastAPI()
        add_forwarding_middleware(self.app)
        self.app.include_router(router)
        self.client = self.enterContext(TestClient(
            self.app, base_url="http://localhost:5002", client=("127.0.0.1", 1234),
        ))
        project = self.root / "projects" / "demo"
        self.write_json(project / ".xo" / "project.json", {"name": "demo", "display_name": "Demo"})
        (project / "PLAN.md").write_text("Next steps", encoding="utf-8")
        self.write_json(project / ".xo" / "todos.json", {"sessions": {"session": {"todos": [
            {"id": "one", "content": "First step", "status": "pending"},
        ]}}})

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def enable(self):
        response = self.client.put("/api/cli-access", json={"enabled": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()["token"]

    def call(self, path, token):
        return self.client.get(path, headers={"Authorization": f"Bearer {token}"})

    def test_default_disabled_and_strict_management(self):
        status = self.client.get("/api/cli-access").json()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["download_path"], "/api/cli-access/client")
        self.assertEqual(self.client.post("/api/cli-access/rotate-token").status_code, 409)
        for body in ({}, {"enabled": "true"}, {"enabled": 1}, {"enabled": True, "token": "chosen"}):
            self.assertEqual(self.client.put("/api/cli-access", json=body).status_code, 422)
        self.assertFalse(cli_access.settings.path().exists())

    def test_cli_runs_all_commands_while_mcp_stays_disabled(self):
        token = self.enable()
        self.assertFalse(mcp_store.read()["enabled"])
        self.assertFalse(mcp_store.config_path().exists())
        expected = {
            "/api/cli/status": "commands",
            "/api/cli/projects?limit=10&offset=0": "projects",
            "/api/cli/document?project_id=demo&document=PLAN.md": "content",
            "/api/cli/todos?project_id=demo&limit=10": "todos",
            "/api/cli/inbox?status=open&limit=10": "items",
        }
        for path, key in expected.items():
            with self.subTest(path=path):
                response = self.call(path, token)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn(key, response.json())
                self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.call("/api/cli/document?project_id=demo&document=PLAN.md", token).json()["content"], "Next steps")

    def test_every_command_is_gated_and_tokens_do_not_cross_interfaces(self):
        paths = ["/api/cli/status", "/api/cli/projects", "/api/cli/document?project_id=demo&document=PLAN.md",
                 "/api/cli/todos?project_id=demo", "/api/cli/inbox"]
        _, mcp_token = mcp_store.update(enabled=True)
        for path in paths:
            self.assertEqual(self.call(path, mcp_token).status_code, 404)
        cli_token = self.enable()
        for path in paths:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 401)
            self.assertIn("Bearer", response.headers["www-authenticate"])
            self.assertEqual(self.call(path, mcp_token).status_code, 401)
        with self.assertRaises(ServiceError) as error:
            mcp_store.authenticate(f"Bearer {cli_token}")
        self.assertEqual(error.exception.status, 401)
        self.client.put("/api/cli-access", json={"enabled": False})
        mcp_store.authenticate(f"Bearer {mcp_token}")
        fresh = self.enable()
        mcp_store.update(enabled=False)
        self.assertEqual(self.call("/api/cli/status", fresh).status_code, 200)

    def test_one_time_issue_rotation_and_disable_revoke_only_cli(self):
        token = self.enable()
        saved = cli_access.settings.path().read_text()
        self.assertNotIn(token, saved)
        self.assertEqual(cli_access.settings.path().stat().st_mode & 0o777, 0o600)
        self.assertNotIn("token", self.client.get("/api/cli-access").json())
        self.assertNotIn("token", self.client.put("/api/cli-access", json={"enabled": True}).json())
        rotated = self.client.post("/api/cli-access/rotate-token").json()["token"]
        self.assertEqual(self.call("/api/cli/status", token).status_code, 401)
        self.assertEqual(self.call("/api/cli/status", rotated).status_code, 200)
        self.client.put("/api/cli-access", json={"enabled": False})
        self.assertEqual(self.call("/api/cli/status", rotated).status_code, 404)
        self.assertIsNone(cli_access.settings.read()["token_hash"])
        self.assertNotEqual(self.enable(), rotated)

    def test_existing_mcp_settings_need_no_migration(self):
        token = "an-existing-random-credential"
        data = {"schema": 1, "enabled": True, "token_hash": hashlib.sha256(token.encode()).hexdigest()}
        self.write_json(mcp_store.config_path(), data)
        before = mcp_store.config_path().read_bytes()
        mcp_store.authenticate(f"Bearer {token}")
        self.enable()
        self.assertEqual(mcp_store.config_path().read_bytes(), before)
        mcp_store.authenticate(f"Bearer {token}")

    def test_cli_corruption_fails_closed_without_changing_mcp(self):
        _, mcp_token = mcp_store.update(enabled=True)
        token = self.enable()
        cli_access.settings.path().write_text("{broken")
        self.assertEqual(self.call("/api/cli/status", token).status_code, 503)
        self.assertEqual(self.client.put("/api/cli-access", json={"enabled": True}).status_code, 503)
        mcp_store.authenticate(f"Bearer {mcp_token}")

    def test_query_limits_document_allowlist_and_paths_are_enforced(self):
        token = self.enable()
        for path in (
            "/api/cli/projects?limit=101", "/api/cli/projects?offset=-1",
            "/api/cli/inbox?status=deleted", "/api/cli/todos?project_id=demo&limit=0",
            "/api/cli/document?project_id=demo&document=.env",
        ):
            self.assertEqual(self.call(path, token).status_code, 422, path)
        response = self.call("/api/cli/document?project_id=../demo&document=PLAN.md", token)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(str(self.root), response.text)
        with patch.object(space_tools, "list_projects", side_effect=OSError("/private/data")):
            response = self.call("/api/cli/projects", token)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("/private/data", response.text)

    def test_remote_command_access_cannot_manage_or_download(self):
        token = self.enable()
        with TestClient(self.app, base_url="https://space.example", client=("198.51.100.2", 123)) as remote:
            headers = {"Authorization": f"Bearer {token}"}
            for path in ("/api/cli-access", "/api/cli-access/client"):
                self.assertEqual(remote.get(path, headers=headers).status_code, 403)
            self.assertEqual(remote.put("/api/cli-access", json={"enabled": False}, headers=headers).status_code, 403)
            self.assertEqual(remote.get("/api/cli/projects", headers=headers).status_code, 200)
        headers = {"Authorization": f"Bearer {token}", "Origin": "http://evil.example", "Host": "evil.example"}
        self.assertEqual(self.client.get("/api/cli/status", headers=headers).status_code, 403)
        self.assertEqual(self.client.get("/api/cli-access", headers=headers).status_code, 403)

    def test_download_is_the_standalone_script_and_contains_no_credentials(self):
        token = self.enable()
        response = self.client.get("/api/cli-access/client")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-disposition"], 'attachment; filename="space"')
        self.assertEqual(response.headers["content-type"], "application/octet-stream")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.content, cli_access.client_path().read_bytes())
        self.assertNotIn(token, response.text)

    def test_downloaded_client_configures_and_runs_against_real_routes(self):
        token = self.enable()
        downloaded = self.root / "downloaded-space"
        downloaded.write_bytes(self.client.get("/api/cli-access/client").content)
        loader = importlib.machinery.SourceFileLoader("downloaded_space_cli", str(downloaded))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cli = importlib.util.module_from_spec(spec)
        loader.exec_module(cli)

        def open_request(request, timeout):
            self.assertEqual(timeout, 15)
            # The in-process server's logs are separate from the downloaded
            # client's stdout/stderr in a real network connection.
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                response = self.client.request(request.get_method(), request.full_url, headers=dict(request.header_items()))
            if response.status_code >= 400:
                raise HTTPError(request.full_url, response.status_code, "error", response.headers, io.BytesIO(response.content))
            return io.BytesIO(response.content)

        environment = {
            "SPACE_CLI_CONFIG": str(self.root / "client-config.json"),
            "SPACE_CLI_TOKEN": "", "SPACE_URL": "",
        }
        with patch.dict(os.environ, environment), patch.object(cli, "build_opener", return_value=Mock(open=open_request)), \
                patch.object(cli.getpass, "getpass", return_value=token):
            def invoke(*args):
                output, error = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(error):
                    code = cli.main(list(args))
                self.assertNotIn(token, output.getvalue() + error.getvalue())
                return code, output.getvalue(), error.getvalue()

            self.assertEqual(invoke("configure", "--url", "http://localhost:5002")[0], 0)
            code, output, error = invoke("projects", "--json")
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(json.loads(output)["projects"][0]["project_id"], "demo")
            self.assertEqual(invoke("document", "demo", "PLAN.md")[1].strip(), "Next steps")
            self.assertIn("First step", invoke("todos", "demo")[1])
            self.assertEqual(json.loads(invoke("inbox", "--json")[1])["items"], [])
            self.client.put("/api/cli-access", json={"enabled": False})
            code, output, error = invoke("projects", "--json")
            self.assertEqual((code, output), (1, ""))
            self.assertIn("disabled", error)


if __name__ == "__main__":
    unittest.main()
